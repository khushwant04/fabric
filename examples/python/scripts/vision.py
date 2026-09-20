"""Ask a vision-capable model about a remote or local image."""

import argparse
import base64
import mimetypes
from pathlib import Path

from fabric_examples import Config, fresh_openai_client
from fabric_examples.cli import THINKING_DISABLED, run


def data_url(path: Path) -> str:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{content_type};base64,{base64.b64encode(path.read_bytes()).decode()}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default="Describe this image concisely.")
    parser.add_argument("--image-url", help="Override FABRIC_IMAGE_URL")
    parser.add_argument("--image-file", type=Path, help="Override FABRIC_IMAGE_FILE")
    args = parser.parse_args()
    config = Config.from_env()
    image = (
        data_url(args.image_file or config.image_file)
        if (args.image_file or config.image_file)
        else (args.image_url or config.image_url)
    )
    if not image:
        raise ValueError("set FABRIC_IMAGE_URL/FABRIC_IMAGE_FILE or pass an image option")
    with fresh_openai_client(config) as client:
        result = client.chat.completions.create(
            model=config.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": args.prompt},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ],
            max_tokens=256,
            extra_body=THINKING_DISABLED,
        )
    print(result.choices[0].message.content or "")


if __name__ == "__main__":
    run(main)
