if __name__ == '__main__':
    import os
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'

import os
import shutil
import tempfile
import unittest

import torch

from swift.llm import DatasetName, InferArguments, ModelType, SftArguments
from swift.llm.run import infer_main, merge_lora_main, sft_main


class TestRun(unittest.TestCase):

    def setUp(self):
        print(f'Testing {type(self).__name__}.{self._testMethodName}')
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp_dir.name

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def test_run_1(self):
        output_dir = 'output'
        if not __name__ == '__main__':
            output_dir = self.tmp_dir
        model_type = ModelType.chatglm3_6b
        sft_args = SftArguments(
            model_type=model_type,
            template_type='AUTO',
            quantization_bit=4,
            eval_steps=5,
            check_dataset_strategy='warning',
            train_dataset_sample=200,
            predict_with_generate=False,
            dataset=[DatasetName.jd_sentiment_zh],
            output_dir=output_dir,
            gradient_checkpointing=True)
        output = sft_main(sft_args)
        print(output)
        best_model_checkpoint = output['best_model_checkpoint']
        print(f'best_model_checkpoint: {best_model_checkpoint}')
        torch.cuda.empty_cache()
        if __name__ == '__main__':
            infer_args = InferArguments(
                ckpt_dir=best_model_checkpoint,
                stream=False,
                show_dataset_sample=5)
            merge_lora_main(infer_args)
            result = infer_main(infer_args)
            print(result)
            torch.cuda.empty_cache()
        # if __name__ == '__main__':
        #     web_ui_main(infer_args)

    def test_run_2(self):
        output_dir = 'output'
        if not __name__ == '__main__':
            # ignore citest error in github
            output_dir = self.tmp_dir
            return
        best_model_checkpoint = sft_main([
            '--model_type',
            ModelType.qwen_7b_chat_int4,
            '--eval_steps',
            '5',
            '--tuner_backend',
            'peft',
            '--train_dataset_sample',
            '200',
            '--predict_with_generate',
            'true',
            '--dataset',
            DatasetName.leetcode_python_en,
            '--output_dir',
            output_dir,
            '--use_flash_attn',
            'false',
            '--gradient_checkpointing',
            'true',
            '--max_new_tokens',
            '100',
        ])['best_model_checkpoint']
        print(f'best_model_checkpoint: {best_model_checkpoint}')
        torch.cuda.empty_cache()
        infer_main([
            '--ckpt_dir',
            best_model_checkpoint,
            '--show_dataset_sample',
            '5',
            '--max_new_tokens',
            '100',
        ])


if __name__ == '__main__':
    unittest.main()
