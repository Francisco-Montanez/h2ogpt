import os
import pickle
import sys
import json
import inspect
import threading
import traceback
import typing
import uuid
from traceback import print_exception
from typing import Union

from pydantic import BaseModel

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request, Depends
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sse_starlette import EventSourceResponse
from starlette.responses import PlainTextResponse

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))

if os.path.dirname('src') not in sys.path:
    sys.path.append('src')

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.append(project_root)


# similar to openai_server/server.py
def verify_api_key(authorization: str = Header(None)) -> None:
    server_api_key = os.getenv('H2OGPT_OPENAI_API_KEY', 'EMPTY')
    if server_api_key == 'EMPTY':
        # dummy case since '' cannot be handled
        return
    if server_api_key and (authorization is None or authorization != f"Bearer {server_api_key}"):
        raise HTTPException(status_code=401, detail="Unauthorized")


app = FastAPI()
check_key = [Depends(verify_api_key)]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


class InvalidRequestError(Exception):
    pass


class FunctionRequest(BaseModel):
    function_name: str
    args: tuple
    kwargs: dict
    use_disk: bool = False
    use_pickle: bool = False


@app.get("/health")
async def health() -> Response:
    """Health check."""
    return Response(status_code=200)


@app.exception_handler(Exception)
async def validation_exception_handler(request, exc):
    print_exception(exc)
    exc2 = InvalidRequestError(str(exc))
    return PlainTextResponse(str(exc2), status_code=400)


@app.options("/", dependencies=check_key)
async def options_route():
    return JSONResponse(content="OK")


gen_kwargs = {}
gen_kwargs_lock = threading.Lock()


def initialize_gen_kwargs():
    global gen_kwargs
    with gen_kwargs_lock:  # not strictly required if in global scope
        if not gen_kwargs:
            main_kwargs = json.loads(os.environ['H2OGPT_MAIN_KWARGS'])  # required

            # don't double up LLMs, in pure "document ingest" mode
            main_kwargs['model_lock'] = []
            main_kwargs['base_model'] = ''
            main_kwargs['inference_server'] = ''

            # only for chat part, not used here
            main_kwargs['enable_image'] = False
            main_kwargs['visible_image_models'] = []
            main_kwargs['image_gpu_ids'] = None

            # function server mode only
            main_kwargs['gradio'] = False
            main_kwargs['eval'] = False
            main_kwargs['cli'] = False
            main_kwargs['function'] = True
            # don't double this
            main_kwargs['openai_server'] = False

            # FIXME: Deal with GPU IDs for each caption/ASR/DocTR model, use MIG, etc.

            from src.gen import main as gen_main
            gen_kwargs = gen_main(**main_kwargs)


# Call the initialization function at startup, but not during import
if 'H2OGPT_MAIN_KWARGS' in os.environ:
    initialize_gen_kwargs()
else:
    print("H2OGPT_MAIN_KWARGS not found in os.environ")


@app.post("/execute_function/", dependencies=check_key)
def execute_function(request: FunctionRequest):
    # Mapping of function names to function objects
    from src.gpt_langchain import path_to_docs
    FUNCTIONS = {
        'path_to_docs': path_to_docs,
    }
    try:
        # Fetch the function from the function map
        func = FUNCTIONS.get(request.function_name)
        if not func:
            raise ValueError("Function not found")

        # use gen_kwargs if needed
        func_names = list(inspect.signature(func).parameters)
        func_kwargs = {k: v for k, v in gen_kwargs.items() if k in func_names and k not in request.kwargs}

        # Call the function with args and kwargs
        result = func(*request.args, **request.kwargs, **func_kwargs)

        if request.use_disk:
            # Save the result to a file on the shared disk
            base_path = 'function_results'
            if not os.path.isdir(base_path):
                os.makedirs(base_path)
            file_path = os.path.join(base_path, str(uuid.uuid4()))
            if request.use_pickle:
                file_path += '.pkl'
                with open(file_path, "wb") as f:
                    pickle.dump(result, f)
            else:
                file_path += '.json'
                with open(file_path, "w") as f:
                    json.dump(result, f)
            return {"status": "success", "file_path": file_path}
        else:
            # Return the result directly
            return {"status": "success", "result": result}
    except Exception as e:
        traceback_str = ''.join(traceback.format_exception(e))
        raise HTTPException(status_code=500, detail=traceback_str)
