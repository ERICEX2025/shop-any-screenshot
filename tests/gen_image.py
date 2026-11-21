"""Make a synthetic test photo with gemini-nano-banana-2.1. Usage: gen_image.py out.jpg "prompt" """
import base64, pathlib, sys, time
from dotenv import load_dotenv
load_dotenv(pathlib.Path(__file__).parent.parent / ".env")
from google import genai
beta = genai.Client(http_options={"api_version": "v1beta"})
it = beta.interactions.create(model="gemini-nano-banana-2.1", input=sys.argv[2])
while getattr(it, "status", None) in ("in_progress", "queued"):
    time.sleep(1); it = beta.interactions.get(id=it.id)
pathlib.Path(sys.argv[1]).write_bytes(base64.b64decode(it.output_image.data))
print("saved", sys.argv[1])
