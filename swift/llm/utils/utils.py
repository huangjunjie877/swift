# Copyright (c) Alibaba, Inc. and its affiliates.
# Part of the implementation is borrowed from huggingface/transformers.
import heapq
import logging
import os
import shutil
from functools import wraps
from tempfile import TemporaryDirectory
from typing import (Any, Callable, Dict, Iterator, List, Optional, Tuple, Type,
                    TypeVar, Union)

import accelerate
import numpy as np
import requests
import torch
import torch.distributed as dist
import torch.nn.functional as F
import transformers
from accelerate.utils.modeling import (get_balanced_memory,
                                       infer_auto_device_map)
from datasets import Dataset as HfDataset
from modelscope import MsDataset
from modelscope.utils.config_ds import MS_CACHE_HOME
from modelscope.utils.logger import get_logger as get_ms_logger
from torch import device as Device
from torch.nn import Linear, Module
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.nn.utils.rnn import pad_sequence
from tqdm.auto import tqdm
from transformers import (PreTrainedModel, PreTrainedTokenizerBase,
                          TextStreamer, trainer)

from swift.hub import ModelScopeConfig
from swift.utils import (append_to_jsonl, get_dist_setting, get_logger,
                         is_ddp_plus_mp, is_dist, is_local_master, is_master,
                         parse_args, stat_array, upper_bound)
from .template import History, Template

logger = get_logger()
ms_logger = get_ms_logger()

os.environ['TOKENIZERS_PARALLELISM'] = 'true'


def download_files(url: str, local_path: str, cookies) -> None:
    resp = requests.get(url, cookies=cookies, stream=True)
    with open(local_path, 'wb') as f:
        for data in tqdm(resp.iter_lines()):
            f.write(data)


def download_dataset(model_id: str,
                     files: List[str],
                     force_download: bool = False) -> str:
    url = f'http://www.modelscope.cn/api/v1/datasets/{model_id}/repo?Revision=master&FilePath={{fpath}}'
    cache_dir = os.path.join(MS_CACHE_HOME, 'datasets', model_id, 'master')
    local_dir = os.path.join(cache_dir, 'raw')
    tmp_dir = os.path.join(cache_dir, 'tmp')
    os.makedirs(local_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    cookies = ModelScopeConfig.get_cookies()
    with TemporaryDirectory(dir=tmp_dir) as temp_dir:
        for remote_fpath in files:
            url = url.format(fpath=remote_fpath)
            temp_fpath = os.path.join(temp_dir, remote_fpath)
            local_fpath = os.path.join(local_dir, remote_fpath)
            if not force_download and os.path.exists(local_fpath):
                continue
            download_files(url, temp_fpath, cookies)
            shutil.copy2(temp_fpath, local_fpath)

    return local_dir


_old_msdataset_load = MsDataset.load


@wraps(_old_msdataset_load)
def _msdataset_ddp_load(*args, **kwargs):
    if is_dist() and not is_local_master():
        dist.barrier()
    dataset = _old_msdataset_load(*args, **kwargs)
    if is_dist() and is_local_master():
        dist.barrier()

    if is_dist():
        dist.barrier()
    return dataset


def _get_max_memory(device_ids: List[int]) -> Dict[Union[int, str], int]:
    """add feat in accelerate to support DDP + MP"""
    import psutil
    # Make sure CUDA is initialized on each GPU to have the right memory info.
    for i in device_ids:
        _ = torch.tensor([0], device=i)

    device_ids_set = set(device_ids)
    max_memory = {}
    for i in range(torch.cuda.device_count()):
        max_memory[i] = 0
        if i in device_ids_set:
            max_memory[i] = torch.cuda.mem_get_info(i)[0]
    max_memory['cpu'] = psutil.virtual_memory().available
    return max_memory


def _sync_max_memory(
        max_memory: Dict[Union[int, str], int]) -> Dict[Union[int, str], int]:
    """Make sure that the model structure of MP(device_map) is the same, when using DDP."""
    max_memory_list = [
        v for k, v in max_memory.items() if (v > 0 and k != 'cpu')
    ]
    _, local_rank, world_size, _ = get_dist_setting()
    src_tensor = torch.tensor(max_memory_list).to(local_rank)
    tgt_tensor_list = [torch.zeros_like(src_tensor) for _ in range(world_size)]
    dist.all_gather(tgt_tensor_list, src_tensor)
    tgt_tensor = torch.stack(tgt_tensor_list, dim=0)
    new_max_memory_iter = iter(tgt_tensor.min(dim=0)[0].tolist())
    new_max_memory = {}
    for k, v in max_memory.items():
        new_max_memory[k] = v
        if v > 0 and k != 'cpu':
            new_max_memory[k] = next(new_max_memory_iter)
    return new_max_memory


@wraps(infer_auto_device_map)
def _infer_auto_device_map_patch(
        model: Module,
        max_memory: Optional[Dict[Union[int, str], Union[int, str]]] = None,
        **kwargs) -> Dict[str, Union[int, str, Device]]:
    """The auxiliary function for supports DDP+MP. Monkey Patching.
    add feat in accelerate to support DDP + MP"""
    verbose = kwargs.pop('verbose', False)
    n_gpu = torch.cuda.device_count()
    _, local_rank, _, local_world_size = get_dist_setting()
    device_ids = list(range(local_rank, n_gpu, local_world_size))
    max_memory = _get_max_memory(device_ids)
    max_memory = _sync_max_memory(max_memory)
    max_memory = get_balanced_memory(
        model, max_memory, low_zero=False, **kwargs)
    max_memory = {k: v for k, v in max_memory.items() if v > 0}
    return infer_auto_device_map(model, max_memory, verbose=verbose, **kwargs)


def dataset_map(
    dataset: HfDataset, map_func: Callable[[Dict[str, Any]],
                                           Dict[str, Optional[List[int]]]]
) -> HfDataset:
    # faster than dataset.map
    input_ids = []
    labels = []
    for d in tqdm(dataset):
        d = map_func(d)
        if d is None or d['input_ids'] is None:
            continue
        input_ids.append(d['input_ids'])
        labels.append(d['labels'])
    return HfDataset.from_dict({'input_ids': input_ids, 'labels': labels})


logger_format = logging.Formatter('[%(levelname)s:%(name)s] %(message)s')

logger.handlers[0].setFormatter(logger_format)
ms_logger.handlers[0].setFormatter(logger_format)
if is_master():
    logger.setLevel(logging.INFO)
    ms_logger.setLevel(logging.INFO)
else:
    logger.setLevel(logging.ERROR)
    ms_logger.setLevel(logging.ERROR)

_TArgsClass = TypeVar('_TArgsClass')
_T = TypeVar('_T')
NoneType = type(None)


def get_main(
    args_class: Type[_TArgsClass], llm_x: Callable[[_TArgsClass], _T]
) -> Callable[[Union[List[str], _TArgsClass, NoneType]], _T]:

    def x_main(argv: Union[List[str], _TArgsClass, NoneType] = None,
               **kwargs) -> _T:
        if isinstance(argv, args_class):
            args, remaining_argv = argv, []
        else:
            args, remaining_argv = parse_args(args_class, argv)
        if len(remaining_argv) > 0:
            if args.ignore_args_error:
                logger.warning(f'remaining_argv: {remaining_argv}')
            else:
                raise ValueError(f'remaining_argv: {remaining_argv}')
        return llm_x(args, **kwargs)

    return x_main


def stat_dataset(dataset: HfDataset) -> None:
    """Statistical analysis was performed on the dataset"""
    _token_len = []
    input_ids = dataset['input_ids']
    for i in range(len(dataset)):
        _token_len.append(len(input_ids[i]))
    _, stat_str = stat_array(_token_len)
    logger.info(f'Dataset Token Length: {stat_str}')


def data_collate_fn(batch: List[Dict[str, Any]],
                    tokenizer: PreTrainedTokenizerBase,
                    padding_to: Optional[int] = None) -> Dict[str, Any]:
    """
    Args:
        batch(`List[Dict[str, Any]]`): The input data in batch
        tokenizer(`PreTrainedTokenizerBase`): The tokenizer of the model
        padding_to(`int`, optional): Whether padding the batch to a fixed length, if none, the batch
            will be padded to the `longest`
    """
    assert tokenizer.pad_token_id is not None
    input_ids = [torch.tensor(b['input_ids']) for b in batch]
    labels = [torch.tensor(b['labels']) for b in batch]
    attention_mask = [
        torch.ones(len(input_ids[i]), dtype=torch.int64)
        for i in range(len(input_ids))
    ]

    if padding_to is not None and padding_to > input_ids[0].shape[-1]:
        input_ids[0] = F.pad(input_ids[0],
                             (0, padding_to - input_ids[0].shape[-1]),
                             'constant', tokenizer.pad_token_id)
        labels[0] = F.pad(labels[0], (0, padding_to - labels[0].shape[-1]),
                          'constant', -100)
        attention_mask[0] = F.pad(
            attention_mask[0], (0, padding_to - attention_mask[0].shape[-1]),
            'constant', 0)

    input_ids = pad_sequence(
        input_ids, batch_first=True, padding_value=tokenizer.pad_token_id)
    attention_mask = pad_sequence(
        attention_mask, batch_first=True, padding_value=0)
    labels = pad_sequence(labels, batch_first=True, padding_value=-100)

    return {
        'input_ids': input_ids,
        'attention_mask': attention_mask,
        'labels': labels
    }


def _get_labels_str(labels: List[int],
                    tokenizer: PreTrainedTokenizerBase) -> str:
    if len(labels) == 0:
        return ''
    labels_str = ''
    for i in range(len(labels)):
        if i == 0:
            if labels[i] == -100:
                s = 0
            else:
                e = 0
            continue
        if labels[i] == -100 and labels[i - 1] != -100:
            s = i
            labels_str += tokenizer.decode(labels[e:s])
        if labels[i] != -100 and labels[i - 1] == -100:
            e = i
            labels_str += f'[-100 * {e - s}]'
    if labels[-1] == -100:
        labels_str += f'[-100 * {len(labels) - s}]'
    else:
        labels_str += tokenizer.decode(labels[e:])
    return labels_str


def print_example(example: Dict[str, Any],
                  tokenizer: PreTrainedTokenizerBase) -> None:
    input_ids, labels = example['input_ids'], example.get('labels')
    logger.info(f'[INPUT_IDS] {input_ids}')
    logger.info(f'[INPUT] {tokenizer.decode(input_ids)}')
    if labels is not None:
        logger.info(f'[LABLES_IDS] {labels}')
        labels_str = _get_labels_str(labels, tokenizer)
        logger.info(f'[LABLES] {labels_str}')


def find_all_linear_for_lora(model: Module, quantization_bit: int,
                             model_type: str) -> List[str]:
    """ref: https://github.com/artidoro/qlora"""
    head_module_name = 'lm_head'
    if model_type.startswith('chatglm'):
        head_module_name = 'output_layer'
    if quantization_bit == 4:
        from bitsandbytes.nn import Linear4bit
        linear_cls = Linear4bit
    elif quantization_bit == 8:
        from bitsandbytes.nn import Linear8bitLt
        linear_cls = Linear8bitLt
    else:
        linear_cls = Linear
    if 'int4' in model_type or 'int8' in model_type:
        from bitsandbytes.nn import Linear4bit
        from peft.utils import get_auto_gptq_quant_linear, get_quantization_config
        gptq_quantization_config = get_quantization_config(model, 'gptq')
        AutoGPTQQuantLinear = get_auto_gptq_quant_linear(
            gptq_quantization_config)
        linear_cls = Linear4bit
        if AutoGPTQQuantLinear is not None:
            linear_cls = (Linear4bit, AutoGPTQQuantLinear)
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, linear_cls):
            module_name = name.split('.')[-1]
            if head_module_name not in module_name:
                lora_module_names.add(module_name)
    return list(lora_module_names)


def sort_by_max_length(dataset: HfDataset, num_dataset: int) -> HfDataset:
    logger.info('sort by max length...')
    input_ids = dataset['input_ids']
    dataset_len = [len(input_ids[i]) for i in range(len(dataset))]
    idx = heapq.nlargest(
        num_dataset, range(len(dataset_len)), key=lambda i: dataset_len[i])
    input_ids = []
    labels = []
    for i in idx:
        input_ids.append(dataset[i]['input_ids'])
        labels.append(dataset[i]['labels'])
    return HfDataset.from_dict({'input_ids': input_ids, 'labels': labels})


def inference_stream(
        model: PreTrainedModel,
        template: Template,
        query: str,
        history: Optional[History] = None,
        system: Optional[str] = None) -> Iterator[Tuple[str, History]]:
    if history is None:
        history = []
    example = {'query': query, 'history': history, 'system': system}
    input_ids = template.encode(example)['input_ids']
    tokenizer = template.tokenizer
    device = next(model.parameters()).device
    input_ids = torch.tensor(input_ids)[None].to(device)
    attention_mask = torch.ones_like(input_ids).to(device)
    model.eval()
    generation_config = getattr(model, 'generation_config', None)
    from transformers_stream_generator.main import NewGenerationMixin, StreamGenerationConfig
    model.__class__.generate_stream = NewGenerationMixin.generate
    model.__class__.sample_stream = NewGenerationMixin.sample_stream
    stream_config = StreamGenerationConfig(
        **generation_config.to_dict(), do_stream=True)
    if stream_config.max_new_tokens is not None:
        stream_config.max_length = 20  # fix max_length, max_new_tokens warning
    stream_config.do_sample = True  # avoid is_greedy_gen_mode = True
    gen = model.generate_stream(
        input_ids=input_ids,
        attention_mask=attention_mask,
        generation_config=stream_config,
        seed=-1)
    generate_ids = []
    history.append(None)  # dummy
    for token in gen:
        generate_ids.append(token.item())
        response = tokenizer.decode(generate_ids, True)
        history[-1] = (query, response)
        yield response, history


def inference(model: PreTrainedModel,
              template: Template,
              query: Optional[str] = None,
              history: Optional[History] = None,
              system: Optional[str] = None,
              *,
              stream: bool = False,
              verbose: bool = False,
              prompt_prefix: str = '[PROMPT]',
              output_prefix: str = '[OUTPUT]') -> Tuple[str, History]:
    if history is None:
        history = []
    example = {'query': query, 'history': history, 'system': system}
    input_ids = template.encode(example)['input_ids']
    tokenizer = template.tokenizer
    device = next(model.parameters()).device
    input_ids = torch.tensor(input_ids)[None].to(device)
    attention_mask = torch.ones_like(input_ids).to(device)
    model.eval()
    generation_config = getattr(model, 'generation_config', None)
    if stream is True and verbose is False:
        logger.warning(
            'Please set verbose to True to support TextStreamer, or use `inference_stream.`'
        )
        stream = False
    streamer = None
    if stream:
        streamer = TextStreamer(tokenizer, skip_prompt=True)
    if verbose:
        print(
            f'{prompt_prefix}{tokenizer.decode(input_ids[0], False)}{output_prefix}',
            end='')
    if generation_config.max_new_tokens is not None:
        generation_config.max_length = 20  # fix max_length, max_new_tokens warning
    generate_ids = model.generate(
        input_ids=input_ids,
        attention_mask=attention_mask,
        streamer=streamer,
        generation_config=generation_config)
    response = tokenizer.decode(generate_ids[0, len(input_ids[0]):], True)
    if verbose and stream is False:
        print(tokenizer.decode(generate_ids[0, len(input_ids[0]):], False))
    history.append((query, response))
    return response, history


def limit_history_length(template: Template, query: str,
                         history: Optional[History], max_length: int) -> int:
    """binary search"""
    if history is None:
        history = []

    def compute_token_length(history_length: int) -> int:
        assert history_length != 0
        example = {'query': query, 'history': history[-history_length:]}
        input_ids = template.encode(example)['input_ids']
        return len(input_ids)

    history_length = upper_bound(
        0, len(history), lambda mid: compute_token_length(mid) <= max_length)
    old_history = history[:len(history) - history_length]
    history = history[len(history) - history_length:]
    return old_history, history


def save_result_to_jsonl(fpath: str,
                         query: str,
                         response: str,
                         label: Optional[str] = None) -> None:

    obj = {'query': query, 'response': response, 'label': label}
    append_to_jsonl(fpath, obj)


# monkey patching
MsDataset.load = _msdataset_ddp_load
if is_ddp_plus_mp():
    _old_ddp_init = DDP.__init__
    accelerate.accelerator.torch.nn.parallel.DistributedDataParallel.__init__ = (
        lambda self, model, device_ids, output_device, *args, **kwargs:
        _old_ddp_init(self, model, *args, **kwargs))
    transformers.modeling_utils.get_balanced_memory = lambda *args, **kwargs: None
    transformers.modeling_utils.infer_auto_device_map = _infer_auto_device_map_patch
    _old_accelerator_init = trainer.Accelerator.__init__
    trainer.Accelerator.__init__ = (
        lambda self, device_placement=False, *args, **kwargs:
        _old_accelerator_init(
            self, device_placement=device_placement, *args, **kwargs))
    trainer.Accelerator.verify_device_map = lambda *args, **kwargs: False
