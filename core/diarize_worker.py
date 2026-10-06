"""發言者分群的子行程（由 core/diarize.py 啟動，不是給人直接執行的）。

為什麼獨立成行程：
- `lec` 只用標準函式庫、跑在系統 Python；sherpa-onnx 是第三方套件，
  Debian 13 等發行版的 PEP 668 不讓人 pip install 進系統 Python，所以它只能
  放在專案 .venv，由父行程找到那個直譯器再啟動本檔。
- 取消時父行程直接結束本行程，不必依賴 sherpa 回呼的語意。
- 三小時錄音的分群會吃掉上 GB 記憶體，行程一結束就全數釋放。

只依賴標準函式庫與 sherpa_onnx。

協定：
  stdin  一個 JSON 物件：
         {"audio", "segmentation_model", "embedding_model", "num_speakers",
          "threshold", "window_shift", "threads"}
  stdout 每行一個 JSON：
         {"type": "progress", "processed": 秒, "total": 秒}
         {"type": "result", "duration": 秒, "engine_version": 字串,
          "turns": [[start, end, cluster, conf], ...]}
         {"type": "error", "code": 穩定代碼, "message": 文字}
  結束代碼：成功 0；有送出 error 事件 2；其他未預期失敗 1。
"""
import array
import json
import subprocess
import sys

SR = 16000


def emit(**event):
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(code, message):
    emit(type="error", code=code, message=message)
    return 2


def decode(path):
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", path,
             "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise RuntimeError(f"decode_failed|ffmpeg 無法執行：{e}")
    if p.returncode != 0:
        tail = (p.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        raise RuntimeError(f"decode_failed|ffmpeg 解碼失敗：{tail[:200]}")
    samples = array.array("f")
    samples.frombytes(p.stdout[:len(p.stdout) // 4 * 4])
    if not samples:
        raise RuntimeError("decode_failed|來源音檔解碼後是空的")
    return samples


def main():
    try:
        req = json.loads(sys.stdin.read())
    except ValueError as e:
        return fail("input_invalid", f"worker 收到無法解析的請求：{e}")

    try:
        import sherpa_onnx as so
    except ImportError as e:
        return fail("engine_unavailable", f"{sys.executable} 沒有安裝 sherpa-onnx：{e}")

    try:
        samples = decode(req["audio"])
    except RuntimeError as e:
        code, _, msg = str(e).partition("|")
        return fail(code, msg)

    duration = len(samples) / SR
    try:
        cfg = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=req["segmentation_model"],
                    window_shift_ratio=req["window_shift"]),
                num_threads=req["threads"]),
            embedding=so.SpeakerEmbeddingExtractorConfig(
                model=req["embedding_model"], num_threads=req["threads"]),
            clustering=so.FastClusteringConfig(
                num_clusters=req["num_speakers"], threshold=req["threshold"],
                compute_confidence=True),
            min_duration_on=0.3, min_duration_off=0.5)
        if not cfg.validate():
            return fail("input_invalid", "sherpa diarization 設定無效（模型路徑或參數）")
        sd = so.OfflineSpeakerDiarization(cfg)
    except Exception as e:
        return fail("engine_unavailable", f"sherpa diarization 初始化失敗：{e}")

    emit(type="progress", processed=0.0, total=duration)

    def on_progress(done, whole):
        emit(type="progress", processed=duration * done / max(whole, 1), total=duration)
        return 0

    try:
        res = sd.process(samples, callback=on_progress)
    except Exception as e:
        return fail("segmentation_failed", f"分群失敗：{e}")

    turns = [[float(s.start), float(s.end), int(s.speaker),
              float(getattr(s, "confidence", 0.0) or 0.0)]
             for s in res.sort_by_start_time()]
    emit(type="result", duration=duration, turns=turns,
         engine_version=str(getattr(so, "__version__", "unknown")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:        # 父行程取消後 pipe 被關掉，安靜結束
        sys.exit(1)
