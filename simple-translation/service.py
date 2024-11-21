import bentoml
import json
import os
import typing as t
import uuid

from pathlib import Path
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse

import bentoml
from bentoml.models import HuggingFaceModel


MAX_TOKENS = 8192
MAX_NEW_TOKENS = 2048
TRANSLATE_PROMPT = """You are a translation machine. Your sole function is to translate the input text from English to Spanish.
Do not add, omit, or alter any information.
Do not provide explanations, opinions, or any additional text beyond the direct translation.
You are not aware of any other facts, knowledge, or context beyond translation between English and Chinese.
Translate the entire input text from their turn.
Example interaction:
User: Very well. Would you like to have a coffee with me?
Assistant: Muy bien. ¿Quieres tomar un café conmigo?
"""

# MODEL_ID = "hugging-quants/Meta-Llama-3.1-70B-Instruct-AWQ-INT4"
MODEL_ID = "meta-llama/Meta-Llama-3.1-8B-Instruct"


app = FastAPI()

@app.post("/start_call")
async def start_call():
    print("POST TwiML")
    service_url = os.environ.get("BENTOCLOUD_DEPLOYMENT_URL")
    assert(service_url)
    if service_url.startswith("http"):
        from urllib.parse import urlparse
        service_url = urlparse(service_url).netloc
    tmpl = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay url="wss://{service_url}/translate/ws" welcomeGreeting="Hi! I'm Jane from Bento M L. I will translate English into Spanish for you!"></ConversationRelay>
  </Connect>
</Response>
    """
    return HTMLResponse(content=tmpl.format(service_url=service_url), media_type="application/xml")

@bentoml.service(
    traffic={"timeout": 360},
    resources={
        "gpu": 1,
        "gpu_type": "nvidia-a100-80gb",
    },
)
@bentoml.mount_asgi_app(app, path="/translate")
class TwilioTranslateBot:

    model_ref = HuggingFaceModel(MODEL_ID)

    def __init__(self):
        from transformers import AutoTokenizer
        from vllm import AsyncEngineArgs, AsyncLLMEngine

        engine_args = AsyncEngineArgs(
            model=self.model_ref,
            max_model_len=MAX_TOKENS,
            enable_prefix_caching=True,
        )

        self.engine = AsyncLLMEngine.from_engine_args(engine_args)
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_ref)


    @app.websocket("/ws")
    async def websocket_endpoint(self, websocket: WebSocket):

        await websocket.accept()

        input_buffer = []
        while True:
            data = await websocket.receive_json()
            if data["type"] == "prompt":
                input_buffer.append(data["voicePrompt"])
                if not data["last"]:
                    continue
            else:
                continue

            prompt = " ".join(input_buffer)
            input_buffer = []

            from vllm import SamplingParams

            sampling_param = SamplingParams(max_tokens=MAX_NEW_TOKENS)

            messages = [
                {"role": "system", "content": TRANSLATE_PROMPT},
                {"role": "user", "content": prompt},
            ]
            prompt = self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True,
            )
            stream = await self.engine.add_request(
                uuid.uuid4().hex, prompt, sampling_param,
            )

            cursor = 0
            async for request_output in stream:
                text = request_output.outputs[0].text
                out = text[cursor:]
                out_d = {
                    "type": "text",
                    "token": out,
                    "lang": "es-ES",
                    "last": False,
                }
                await websocket.send_json(out_d)
                cursor = len(text)

            last_d = {
                "type": "text",
                "token": "",
                "lang": "es-ES",
                "last": True,
            }

            await websocket.send_json(last_d)
