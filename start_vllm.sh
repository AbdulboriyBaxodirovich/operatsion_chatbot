#!/bin/bash
cd ~/brb-bank-chatbot
source vllm_env/bin/activate

echo "🚀 BRB Bank vLLM ishga tushmoqda... (port 8002)"

vllm serve ./brb_bank_model \
  --port 8002 \
  --max-model-len 8192 \
  --gpu-memory-utilization 0.85 \
  --enforce-eager \
  --dtype auto
