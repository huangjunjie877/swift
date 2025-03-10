# Experimental environment: 2 * A10
# 2 * 13GB GPU memory
nproc_per_node=2

PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0,1 \
torchrun \
    --nproc_per_node=$nproc_per_node \
    --master_port 29500 \
    llm_sft.py \
    --model_id_or_path damo/nlp_seqgpt-560m \
    --model_revision master \
    --sft_type full \
    --template_type default-generation \
    --dtype AUTO \
    --output_dir output \
    --ddp_backend nccl \
    --dataset ner-jave-zh \
    --train_dataset_sample -1 \
    --num_train_epochs 3 \
    --max_length 1024 \
    --check_dataset_strategy warning \
    --gradient_checkpointing true \
    --batch_size 4 \
    --weight_decay 0.01 \
    --learning_rate 2e-5 \
    --gradient_accumulation_steps $(expr 32 / $nproc_per_node / 4) \
    --max_grad_norm 1 \
    --warmup_ratio 0.03 \
    --eval_steps 100 \
    --save_steps 100 \
    --only_save_model false \
    --save_total_limit 2 \
    --logging_steps 10 \
    --push_to_hub false \
    --hub_model_id seqgpt-560m-full \
    --hub_private_repo true \
    --hub_token 'your-sdk-token' \
