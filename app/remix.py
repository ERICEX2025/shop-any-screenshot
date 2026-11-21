"""Remix: change one item in a screenshot (colour, material, shape) and get a search query for the real thing.

  suggest  Gemini 4 Hydrogen proposes a few variations that make sense for this kind of item.
  render   Gemini's image model redraws the screenshot with only that item changed.
"""
import base64
import json
import time

from google import genai

HYDROGEN, IMAGE = "gemini-4-hydrogen", "gemini-nano-banana-2.1"
beta = genai.Client(http_options={"api_version": "v1beta"})

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {"variations": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "2 or 3 words shown on a button, e.g. 'Olive velvet'"},
            "kind": {"type": "string", "enum": ["colour", "material", "shape", "style"]},
            "edit": {"type": "string", "description": "How the item should look, as an instruction to an image editor"},
            "query": {"type": "string", "description": "Shopping search query for the changed item, 3 to 6 words"},
        },
        "required": ["label", "kind", "edit", "query"]}}},
    "required": ["variations"],
}


def _wait(it):
    while getattr(it, "status", None) in ("in_progress", "queued"):
        time.sleep(1)
        it = beta.interactions.get(id=it.id)
    return it


def _img(data, mime="image/jpeg"):
    return {"type": "image", "data": base64.b64encode(data).decode(), "mime_type": mime}


def suggest(image, item, mime="image/jpeg"):
    """Four variations a shopper would plausibly want: mix colours, a material and a shape."""
    it = _wait(beta.interactions.create(
        model=HYDROGEN,
        input=[_img(image, mime), {"type": "text", "text":
            f"The shopper likes the {item} in this picture and wants to see alternatives. Suggest 4 variations of it that are "
            "really sold: two colours that suit the scene, one different material or finish, and one different shape or cut. "
            "Do not suggest the look it already has."}],
        response_format={"type": "text", "mime_type": "application/json", "schema": SUGGEST_SCHEMA},
        generation_config={"thinking_level": "low"}))
    return json.loads(it.output_text)["variations"][:4]


def custom(item, wish):
    """A variation the shopper typed themselves, e.g. 'make it corduroy'."""
    return {"label": wish[:24], "kind": "style", "edit": f"{wish}", "query": f"{wish} {item}"[:60]}


def render(image, item, edit, mime="image/jpeg", ref=None):
    """Redraw the picture with only that item changed. Returns (bytes, mime).
    ref: optional JPEG bytes of a merchant's product photo; the changed item should then look like that real product."""
    parts = [_img(image, mime)]
    text = (f"Edit this image. Change only the {item}: {edit}. Keep the same camera angle, lighting, people, background and "
            "every other object exactly as they are. Photorealistic, no added text. "
            "Return the WHOLE image with the same framing, crop and aspect ratio as the input: if it is a screenshot, keep the "
            "app interface, captions, text and borders around the photo pixel for pixel. Do not zoom, extend or recompose.")
    if ref:
        parts.append(_img(ref))
        text = ("The FIRST image is the scene to edit. The SECOND image is a store's photo of a real product and is only a "
                "reference for colour, material and finish. " + text + " Take the colour, material and finish from the "
                "reference photo. Output the edited first image only; do not paste in the reference photo or its background.")
    it = _wait(beta.interactions.create(
        model=IMAGE,
        input=parts + [{"type": "text", "text": text}],
        response_modalities=["image"]))
    out = it.output_image
    return base64.b64decode(out.data), getattr(out, "mime_type", "image/jpeg")
