import asyncio
import os
import uuid

from fastapi import FastAPI, WebSocket
from starlette.responses import HTMLResponse

import bentoml
from bentoml.models import HuggingFaceModel


MAX_TOKENS = 8192
MAX_NEW_TOKENS = 2048
SYSTEM_PROMPT = """You are a helpful, respectful and honest assistant. You can hear and speak. You are chatting with a user over voice. Your voice and personality should be warm and engaging, with a lively and playful tone, full of charm and energy. The content of your responses should be conversational, nonjudgmental, and friendly.

Always answer as helpfully as possible, while being safe. Your answers should not include any harmful, unethical, racist, sexist, toxic, dangerous, or illegal content. Please ensure that your responses are socially unbiased and positive in nature.

If a question does not make any sense, or is not factually coherent, explain why instead of answering something not correct. If you don't know the answer to a question, please don't share false information."""

MODEL_ID = "hugging-quants/Meta-Llama-3.1-70B-Instruct-AWQ-INT4"


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
    <ConversationRelay url="wss://{service_url}/chat/ws" welcomeGreeting="Hi! I'm Jane from Bento M L. Just chat with me!!"></ConversationRelay>
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
@bentoml.mount_asgi_app(app, path="/chat")
class TwilioChatBot:

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
        queue = asyncio.queues.Queue()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
        ]

        # function in charge of getting replies from LLM and updating
        # the message history. This function could be canceled by user
        # interruption.
        async def llm_request(message):
            from vllm import SamplingParams

            sampling_param = SamplingParams(max_tokens=MAX_NEW_TOKENS)
            prompt = self.tokenizer.apply_chat_template(
                messages + [dict(role="user", content=message)],
                tokenize=False, add_generation_prompt=True,
            )
            request_id = uuid.uuid4().hex
            replies = []

            try:
                stream = await self.engine.add_request(
                    request_id, prompt, sampling_param
                )

                cursor = 0
                async for request_output in stream:
                    text = request_output.outputs[0].text
                    out = text[cursor:]
                    replies.append(out)

                    out_d = {
                        "type": "text",
                        "token": out,
                        "last": False,
                    }
                    await websocket.send_json(out_d)
                    cursor = len(text)

            except asyncio.CancelledError:
                await self.engine.abort(request_id)
                raise

            finally:

                # update both Q&A message history in finally block to make it atomic
                reply = "".join(replies)
                messages.append(dict(role="user", content=message))
                messages.append(dict(role="assistant", content=reply))
                print(messages)

                last_d = {
                    "type": "text",
                    "token": "",
                    "last": True,
                }

                await websocket.send_json(last_d)

        async def read_from_socket(websocket: WebSocket):
            async for data in websocket.iter_json():
                queue.put_nowait(data)

        # function in charge of fetching data from queue, launching
        # llm tasks and canceling them when interrupted
        async def get_data_and_process():
            input_buffer = []
            llm_task = None

            while True:
                data = await queue.get()
                if data["type"] == "prompt":
                    input_buffer.append(data["voicePrompt"])
                    if not data["last"]:
                        continue

                elif data["type"] == "interrupt":
                    input_buffer = []
                    if llm_task:
                        llm_task.cancel()
                        try:
                            await llm_task
                        except asyncio.CancelledError:
                            pass
                        llm_task = None
                else:
                    continue

                message = " ".join(input_buffer)
                input_buffer = []
                llm_task = asyncio.create_task(llm_request(message))
                
        await asyncio.gather(read_from_socket(websocket), get_data_and_process())
