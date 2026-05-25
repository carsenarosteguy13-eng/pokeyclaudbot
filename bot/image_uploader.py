import base64
import requests
from config import IMGBB_API_KEY


def upload_image(image_bytes: bytes) -> str:
    """Upload image bytes to imgbb and return a permanent public HTTPS URL."""
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    response = requests.post(
        "https://api.imgbb.com/1/upload",
        data={"key": IMGBB_API_KEY, "image": image_b64},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"Imgbb upload failed: {data}")
    return data["data"]["url"]
