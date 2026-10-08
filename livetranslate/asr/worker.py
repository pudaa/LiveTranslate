import gc
import inspect
import logging
import sys
import traceback
from typing import Any

import numpy as np

from livetranslate.asr import registry

log = logging.getLogger("LiveTranslate.ASRWorker")


def _setup_logging():
    if logging.getLogger().handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("LiveTranslate").setLevel(logging.DEBUG)


def _error_response(msg_id: str | None, exc: BaseException, recoverable: bool) -> dict:
    return {
        "id": msg_id,
        "ok": False,
        "type": "error",
        "error": {
            "message": str(exc),
            "traceback": traceback.format_exc(),
            "recoverable": recoverable,
        },
    }


def _ok_response(msg_id: str | None, response_type: str, payload: Any = None) -> dict:
    return {
        "id": msg_id,
        "ok": True,
        "type": response_type,
        "payload": payload,
    }


def _load_engine(config: dict):
    """Instantiate the configured backend inside this worker process.

    Resolution goes through ``livetranslate.asr.registry``, which imports only
    the requested backend module -- see that module for the engine contract and
    for how to add a new backend.
    """
    from livetranslate.models.manager import apply_cache_env

    # Must run before the backend imports torch, so TORCH_HOME is respected.
    apply_cache_env()

    engine = registry.build(config.get("engine_type"), config)
    language = config.get("language", "auto")
    if hasattr(engine, "set_language"):
        engine.set_language(language)
    return engine


def _transcribe(engine, payload: dict):
    audio = payload.get("audio")
    if not isinstance(audio, np.ndarray):
        raise TypeError("transcribe payload audio must be a numpy.ndarray")

    kwargs = {}
    signature = inspect.signature(engine.transcribe)
    if "word_timestamps" in signature.parameters:
        kwargs["word_timestamps"] = bool(payload.get("word_timestamps", False))
    return engine.transcribe(audio, **kwargs)


def _cleanup_engine(engine):
    if engine is not None and hasattr(engine, "unload"):
        try:
            engine.unload()
        except Exception:
            log.warning("ASR engine unload failed", exc_info=True)
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def worker_main(conn, config: dict):
    _setup_logging()
    engine = None
    try:
        log.info(
            "ASR worker loading: "
            f"{config.get('engine_type')} on {config.get('device')} "
            f"(pid config={config.get('display_name', '')})"
        )
        engine = _load_engine(config)
        conn.send(
            _ok_response(
                None,
                "ready",
                {
                    "engine_type": config.get("engine_type"),
                    "display_name": config.get("display_name"),
                    "device": config.get("device"),
                },
            )
        )
    except BaseException as exc:
        log.error(f"ASR worker load failed: {exc}", exc_info=True)
        try:
            conn.send(_error_response(None, exc, recoverable=False))
        finally:
            _cleanup_engine(engine)
            conn.close()
        return

    try:
        while True:
            try:
                msg = conn.recv()
            except EOFError:
                break

            msg_id = msg.get("id")
            msg_type = msg.get("type")
            payload = msg.get("payload") or {}

            try:
                if msg_type == "shutdown":
                    conn.send(_ok_response(msg_id, "shutdown"))
                    break
                if msg_type == "transcribe":
                    result = _transcribe(engine, payload)
                    conn.send(_ok_response(msg_id, "result", result))
                    continue
                if msg_type == "set_language":
                    if hasattr(engine, "set_language"):
                        engine.set_language(payload.get("language", "auto"))
                    conn.send(_ok_response(msg_id, "ack"))
                    continue
                if msg_type == "set_input_padding":
                    if hasattr(engine, "set_input_padding"):
                        engine.set_input_padding(payload.get("pad_seconds"))
                    conn.send(_ok_response(msg_id, "ack"))
                    continue
                raise ValueError(f"Unknown ASR worker command: {msg_type}")
            except Exception as exc:
                log.error(f"ASR worker command failed: {msg_type}: {exc}", exc_info=True)
                conn.send(_error_response(msg_id, exc, recoverable=True))
    finally:
        _cleanup_engine(engine)
        try:
            conn.close()
        except Exception:
            pass
        log.info("ASR worker stopped")
