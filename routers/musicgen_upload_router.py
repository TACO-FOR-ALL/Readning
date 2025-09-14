from fastapi import APIRouter, UploadFile, File, Form, HTTPException

import json
import os
import logging
from typing import List

from services import (
    chunk_text_by_emotion,
    prompt_service,
    musicgen_service,
    merge_service,
)
from services.repeat_track import repeat_clips_to_length
from utils.file_utils import save_text_to_file, ensure_dir, secure_filename
from config import OUTPUT_DIR, GEN_DURATION


router = APIRouter(prefix="/generate", tags=["UploadWorkflow"])
logger = logging.getLogger(__name__)


def get_output_path(user_id: str, book_dir: str, chapter: int) -> str:
    return os.path.join(OUTPUT_DIR, user_id, book_dir, f"ch{chapter}.wav")


@router.post("/music-v3-long", summary="Generate long BGM for one page")
async def generate_music_long(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    book_title: str = Form(...),
    page: int = Form(..., ge=1),
    preference: str = Form("[]"),
    target_len: int = Form(240),
):

    # 1) Cache check -------------------------------------------------
    safe_title = secure_filename(book_title)
    out_wav = get_output_path(user_id, safe_title, page)
    if os.path.exists(out_wav):
        logger.info(f"[SKIP] {out_wav} already exists – returning cached url")
        return {
            "message": "cached",
            "download_url": f"/{OUTPUT_DIR}/{user_id}/{safe_title}/ch{page}.wav",
            "page": page,
        }

    # 2) Read text with robust decoding ------------------------------
    try:
        text = file.file.read().decode("utf-8")
    except UnicodeDecodeError:
        try:
            file.file.seek(0)
            text = file.file.read().decode("cp949")
        except UnicodeDecodeError:
            file.file.seek(0)
            text = file.file.read().decode("latin1")
    if not text:
        raise HTTPException(400, "업로드된 파일이 비어 있습니다.")
    if target_len <= 0:
        raise HTTPException(400, "target_len 은 0보다 커야 합니다")

    # 3) Output dirs and temp files ---------------------------------
    book_dir = os.path.join(user_id, safe_title)  # uid/책제목
    abs_bookdir = os.path.join(OUTPUT_DIR, book_dir)
    ensure_dir(abs_bookdir)

    save_text_to_file(os.path.join(abs_bookdir, f"ch{page}.txt"), text)
    tmp_path = os.path.join(abs_bookdir, f"ch{page}_tmp.txt")
    save_text_to_file(tmp_path, text)

    # 4) Preference parsing -----------------------------------------
    try:
        pref_list: List[str] = json.loads(preference)
        if not isinstance(pref_list, list):
            raise ValueError
    except Exception:
        logger.warning("[music-v3-long] preference 파싱 실패: 기본값 [] 사용")
        pref_list = []

    # 5) Chunking ----------------------------------------------------
    try:
        chunks = chunk_text_by_emotion.chunk_text_by_emotion(tmp_path)
    except Exception as e:
        print("[music-v3-long] chunking error:", e)
        raise HTTPException(500, "텍스트 청크 분할 중 오류가 발생했습니다")

    # 6) MusicGen prompts and generation -----------------------------
    try:
        global_prompt = prompt_service.generate_global(text)
        regional_prompts: List[str] = []
        for chunk in chunks:
            ctxt = chunk[0] if isinstance(chunk, (list, tuple)) else chunk
            regional = prompt_service.generate_regional(ctxt)
            pref_line = f"User preference: {', '.join(pref_list)}" if pref_list else ""
            regional_prompts.append(
                prompt_service.compose_musicgen_prompt(
                    global_prompt, f"{regional}\n{pref_line}"
                )
            )
        musicgen_service.generate_music_samples(
            global_prompt=global_prompt,
            regional_prompts=regional_prompts,
            book_id_dir=book_dir,
        )
    except Exception as e:
        print("[music-v3-long] musicgen error:", e)
        raise HTTPException(500, "음악 생성 중 오류가 발생했습니다")

    # 7) Merge and exact-length repeat -------------------------------
    output_filename = f"ch{page}.wav"
    try:
        merge_service.build_and_merge_clips_with_repetition(
            text_chunks=chunks,
            base_output_dir=OUTPUT_DIR,
            book_id_dir=book_dir,
            output_name=output_filename,
            clip_duration=GEN_DURATION,
            total_duration=target_len,
            fade_ms=1500,
        )
        repeat_clips_to_length(
            folder=abs_bookdir,
            base_name="regional_output_",
            clip_duration=None,  # auto-detect per first clip
            target_sec=target_len,
            crossfade_ms=1000,
            output_name=output_filename,
        )
    except Exception as e:
        print("[music-v3-long] merge/repeat error:", e)
        raise HTTPException(500, "오디오 병합 또는 반복 처리 중 오류가 발생했습니다")

    # 8) Response ----------------------------------------------------
    return {
        "message": f"{safe_title} p{page} 음원 생성 완료",
        "download_url": f"/{OUTPUT_DIR}/{user_id}/{safe_title}/{output_filename}",
        "page": page,
    }

