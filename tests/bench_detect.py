"""Compare detection latency/quality across models, thinking levels and image sizes. Usage: bench_detect.py image.jpg"""
import base64, io, json, sys, time
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from app import server as s

img = Image.open(sys.argv[1]).convert("RGB")
def b64(maxpx):
    im = img.copy(); im.thumbnail((maxpx, maxpx)); b = io.BytesIO(); im.save(b, "JPEG", quality=85)
    return base64.b64encode(b.getvalue()).decode()

def run(cfg):
    model, level, px = cfg
    t = time.time()
    it = s._wait(s.beta.interactions.create(
        model=model, input=[{"type": "image", "data": b64(px), "mime_type": "image/jpeg"},
                            {"type": "text", "text": s.DETECT_PROMPT.format(n=1)}],
        response_format={"type": "text", "mime_type": "application/json", "schema": s.DETECT_SCHEMA},
        generation_config={"thinking_level": level}))
    ps = json.loads(it.output_text)["products"]
    return cfg, round(time.time() - t, 1), [p["query"] for p in ps]

cfgs = [("gemini-4-hydrogen", "low", 1280), ("gemini-4-hydrogen", "low", 768), ("gemini-4-hydrogen", "medium", 1280), ("gemini-4-neon", "low", 1280)]
for r in ThreadPoolExecutor(4).map(run, cfgs):
    print(r[0], r[1], "s"); [print("    ", q) for q in r[2]]
