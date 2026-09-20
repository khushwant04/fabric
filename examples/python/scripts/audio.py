"""Transcribe or translate one local audio file (non-streaming)."""

import argparse
from pathlib import Path

from fabric_examples import Config, fresh_openai_client
from fabric_examples.cli import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("transcribe", "translate"))
    parser.add_argument("--file", type=Path, help="Override FABRIC_AUDIO_FILE")
    parser.add_argument("--language", help="ISO language hint for transcription")
    args = parser.parse_args()
    config = Config.from_env()
    path = args.file or config.audio_file
    if path is None:
        raise ValueError("set FABRIC_AUDIO_FILE or pass --file")
    with fresh_openai_client(config) as client, path.open("rb") as audio:
        if args.operation == "transcribe":
            result = client.audio.transcriptions.create(
                model=config.model, file=audio, language=args.language
            )
        else:
            result = client.audio.translations.create(model=config.model, file=audio)
    print(result.text)


if __name__ == "__main__":
    run(main)
