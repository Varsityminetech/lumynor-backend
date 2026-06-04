#!/bin/bash

# Load model name from .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

MODEL=${LOCAL_MODEL_NAME:-"qwen2.5"}

echo "🚀 Lumynor Systems - Local LLM Setup"
echo "Target Model: $MODEL"

if command -v ollama &> /dev/null
then
    echo "✅ Ollama detected. Pulling model..."
    ollama pull $MODEL
    echo "✨ Model $MODEL is ready!"
else
    echo "❌ Ollama is not installed or not in your PATH."
    echo "Please download it from https://ollama.com/download"
    exit 1
fi
