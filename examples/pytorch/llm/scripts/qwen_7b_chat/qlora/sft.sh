# Experimental environment: A10, 3090
# 18GB GPU memory
PYTHONPATH=../../.. \
CUDA_VISIBLE_DEVICES=0 \
python llm_sft.py \
    --model_id_or_path qwen/Qwen-7B-Chat \
    --model_revision master \
    --sft_type lora \
    --tuner_backend swift \
    --template_type chatml \
    --dtype AUTO \
    --output_dir output \
    --dataset damo-agent-mini-zh \
    --train_dataset_sample 20000 \
    --num_train_epochs 1 \
    --max_length 4096 \
    --check_dataset_strategy warning \
    --quantization_bit 4 \
    --bnb_4bit_comp_dtype AUTO \
    --lora_rank 8 \
    --lora_alpha 32 \
    --lora_dropout_p 0.05 \
    --lora_target_modules ALL \
    --gradient_checkpointing true \
    --batch_size 1 \
    --weight_decay 0.01 \
    --learning_rate 1e-4 \
    --gradient_accumulation_steps 16 \
    --max_grad_norm 0.5 \
    --warmup_ratio 0.03 \
    --eval_steps 100 \
    --save_steps 100 \
    --save_total_limit 2 \
    --logging_steps 10 \
    --use_flash_attn false \
    --push_to_hub false \
    --hub_model_id qwen-7b-chat-qlora \
    --hub_private_repo true \
    --hub_token 'your-sdk-token' \
