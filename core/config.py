"""設定載入：config/default.toml → config/local.toml（本機）→ courses/<課名>.toml → 指令列覆寫。"""
import copy
import json
import math
import os
import re
import string
import tomllib
from datetime import datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = APP_ROOT / "config" / "default.toml"
LOCAL_FILE = APP_ROOT / "config" / "local.toml"          # 這台電腦專屬（不進 git）：麥克風、路徑等
TEMPLATE_FILE = APP_ROOT / "config" / "template.toml"
COURSES_DIR = APP_ROOT / "courses"
MEETINGS_DIR = APP_ROOT / "meetings"
MEETING_SECTIONS = {"meeting", "whisper", "audio", "vad", "transcript", "diarization"}


# 這些表格底下可以自由新增項目（模型、音源清單、課程術語）
OPEN_TABLES = {("models",), ("audio", "sources"), ("whisper", "models"),
               ("summary", "glossary")}

UPSTREAMS_FILE = APP_ROOT / "config" / "upstreams.toml"
UPSTREAM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def load_upstreams():
    """Private registry, deliberately separate from merged/session config."""
    if not UPSTREAMS_FILE.exists():
        return {}
    try:
        with UPSTREAMS_FILE.open("rb") as f:
            raw = tomllib.load(f)
        if set(raw) != {"upstreams"} or not isinstance(raw["upstreams"], dict):
            raise ValueError
        result = {}
        for key, spec in raw["upstreams"].items():
            if not UPSTREAM_ID.fullmatch(key) or key == "local" or not isinstance(spec, dict):
                raise ValueError
            if set(spec) - {"name", "base_url", "model", "api_key", "api_key_env"}:
                raise ValueError
            for field in ("name", "base_url", "model"):
                if not isinstance(spec.get(field), str) or not spec[field].strip():
                    raise ValueError
                if any(ord(c) < 32 for c in spec[field]):
                    raise ValueError
            url = urlsplit(spec["base_url"])
            if (any(c.isspace() for c in spec["base_url"])
                    or url.scheme not in {"http", "https"} or not url.hostname or url.username
                    or url.password or url.query or url.fragment):
                raise ValueError
            _ = url.port
            if "api_key" in spec and "api_key_env" in spec:
                raise ValueError
            for field in ("api_key", "api_key_env"):
                if field in spec and (not isinstance(spec[field], str)
                                      or not spec[field].strip()
                                      or any(ord(c) < 32 for c in spec[field])):
                    raise ValueError
            result[key] = dict(spec)
        return result
    except (OSError, ValueError, TypeError):
        # TOML errors can contain a credential-bearing source line.
        raise ConfigError("config/upstreams.toml 無法讀取或格式不合法；請檢查上游設定") from None


CHOICES = {
    ("summary", "file_mode"): {"after", "off"},
    ("summary", "unverified_terms"): {"mark", "drop", "keep"},
}


class ConfigError(Exception):
    pass


def normalize_work_type(value):
    """Read legacy metadata without treating unknown nonempty types as meetings."""
    if value is None or value == "":
        return "lecture"
    return value if isinstance(value, str) else "unknown"


def meeting_path(meeting_id):
    """Meeting IDs are portable filenames, never external TOML paths."""
    reserved = {"CON", "PRN", "AUX", "NUL",
                *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if (not isinstance(meeting_id, str) or not meeting_id or len(meeting_id) > 100
            or len(meeting_id.encode("utf-8")) > 200
            or meeting_id != meeting_id.strip() or meeting_id.startswith((".", "-"))
            or meeting_id.endswith(".") or re.search(r'[\x00-\x1f\x7f<>:"/\\|?*]', meeting_id)
            or meeting_id.split(".", 1)[0].upper() in reserved):
        raise ConfigError("meeting id 必須是可攜檔名，不可包含路徑、保留字或控制字元")
    path = MEETINGS_DIR / f"{meeting_id}.toml"
    if MEETINGS_DIR.is_symlink() or path.is_symlink():
        raise ConfigError("會議設定不可使用符號連結")
    return path


def meeting_output_parts(value):
    """Validate relative output roots consistently on POSIX and Windows."""
    if (not isinstance(value, str) or not value or PureWindowsPath(value).drive
            or value.startswith(("/", "\\"))):
        raise ConfigError("paths.meeting_output_root 必須是無 .. 的相對路徑")
    parts = re.split(r"[/\\]", value)
    from .util import safe_name
    if any(part in ("", ".", "..") or safe_name(part) != part for part in parts):
        raise ConfigError("paths.meeting_output_root 必須是可攜的非空相對路徑，且不可含 ..")
    return parts


def load_toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} 格式錯誤：{e}") from None


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _unknown_keys(default, over, prefix=()):
    warns = []
    for k, v in over.items():
        path = prefix + (k,)
        if prefix in OPEN_TABLES:
            continue
        if k not in default:
            warns.append(".".join(path))
        elif isinstance(v, dict) and isinstance(default[k], dict):
            warns += _unknown_keys(default[k], v, path)
    return warns


def parse_value(raw):
    """--set 的值依 TOML 語法解析（數字、true/false、陣列），失敗就當字串。"""
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def parse_set(expr):
    if "=" not in expr:
        raise ConfigError(f"--set 格式應為 區塊.鍵=值：{expr}")
    key, raw = expr.split("=", 1)
    parts = [p for p in key.strip().split(".") if p]
    if len(parts) < 2:
        raise ConfigError(f"--set 需要指定區塊，例如 summary.temperature=0.3：{expr}")
    d = cur = {}
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = parse_value(raw.strip())
    return d


def course_path(arg):
    """課名或 .toml 路徑 → (設定檔路徑, 課名)。"""
    p = Path(arg).expanduser()
    if arg.endswith(".toml") and ("/" in arg or p.exists()):
        return p.resolve(), p.stem
    return COURSES_DIR / f"{arg}.toml", arg


EXAMPLES_DIR = COURSES_DIR / "examples"


def create_course_file(name, path=None, from_example=None):
    """從範本（或 courses/examples/ 的範例）建立課程設定檔。"""
    path = Path(path) if path else COURSES_DIR / f"{name}.toml"
    if path.exists():
        raise ConfigError(f"課程設定檔已存在：{path}")
    if from_example:
        src = Path(from_example).expanduser()
        if not src.is_file():
            src = EXAMPLES_DIR / f"{from_example.removesuffix('.toml')}.toml"
        if not src.is_file():
            names = ", ".join(p.stem for p in EXAMPLES_DIR.glob("*.toml")) or "無"
            raise ConfigError(f"找不到範例 {from_example}（可用：{names}）")
        text = re.sub(r'(?m)^name\s*=\s*".*"', f"name = {json.dumps(name, ensure_ascii=False)}",
                      src.read_text(encoding="utf-8"), count=1)
        text = re.sub(r"(?m)^# ===== 課程設定：.*=====$", f"# ===== 課程設定：{name} =====", text, count=1)
    else:
        text = TEMPLATE_FILE.read_text(encoding="utf-8").replace("{name}", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- TOML 輸出
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k):
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _val(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    raise TypeError(f"無法輸出 {type(v)}")


def dump_toml(data, header=""):
    lines = [f"# {header}"] if header else []

    def walk(table, path):
        scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
        tables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
        if path and (scalars or not tables):
            lines.append("")
            lines.append("[" + ".".join(_key(p) for p in path) + "]")
        for k, v in scalars:
            lines.append(f"{_key(k)} = {_val(v)}")
        for k, v in tables:
            walk(v, path + [k])

    walk(data, [])
    return "\n".join(lines).lstrip("\n") + "\n"


def set_local(dotted, value):
    """修改 config/local.toml 的一個設定（lec devices --save、UI 用）。會重寫檔案，註解不保留。"""
    data = load_toml(LOCAL_FILE) if LOCAL_FILE.exists() else {}
    parts = dotted.split(".")
    cur = data
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_FILE.with_suffix(".toml.tmp")
    tmp.write_text(dump_toml(data, "這台電腦專屬的設定（不進 git）；由 lec 寫入，也可手動編輯"),
                   encoding="utf-8")
    tmp.replace(LOCAL_FILE)
    return LOCAL_FILE


# ---------------------------------------------------------------- 設定物件
class Config:
    def __init__(self, data, course_file=None, warnings=None):
        self.data = data
        self.course_file = course_file
        self.warnings = warnings or []

    def __getitem__(self, section):
        return self.data[section]

    def get(self, dotted, default=None):
        cur = self.data
        for p in dotted.split("."):
            if not isinstance(cur, dict) or p not in cur:
                return default
            cur = cur[p]
        return cur

    def state_dir(self):
        v = self.data["paths"]["state_dir"]
        from .platform import default_state_dir
        return default_state_dir() if v in ("", "auto") else self.path(v)

    @staticmethod
    def path(value):
        p = Path(str(value)).expanduser()
        return p if p.is_absolute() else (APP_ROOT / p)

    @property
    def work_type(self):
        return normalize_work_type(self.get("work.type"))

    @property
    def meeting_name(self):
        return self.get("meeting.name", "")

    def diarization_options(self, *, require_speakers=False):
        """Validate the public settings contract, without importing/probing engines."""
        value = self.get("diarization.num_speakers", 0)
        minimum = 1 if require_speakers else 0
        if type(value) is not int or not minimum <= value <= 30:
            raise ConfigError(f"diarization.num_speakers 必須是 {minimum}..30 的整數；0 僅表示尚未指定")
        result = {"requested_speakers": value}
        for key in ("cluster_threshold", "segmentation_window_shift"):
            value = self.get("diarization." + key)
            if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0
                    or (key == "cluster_threshold" and value > 1)):
                raise ConfigError(f"diarization.{key} 必須是有限正數" +
                                  ("且 <= 1" if key == "cluster_threshold" else ""))
            result[key] = value
        for key in ("segmentation_model", "embedding_model"):
            value = self.get("diarization." + key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ConfigError(f"diarization.{key} 必須是非空模型路徑")
            result[key] = self.path(value)
        return result

    def meeting_directory_name(self, now):
        from .util import safe_name
        template = self.get("paths.meeting_session_name")
        try:
            if not isinstance(template, str) or not template.strip():
                raise ValueError("empty template")
            for _, field, _, conversion in string.Formatter().parse(template):
                if field is not None and (field not in ("meeting", "date") or conversion):
                    raise ValueError("unknown field")
            name = template.format(meeting=safe_name(self.meeting_name), date=now)
            if not name.strip() or "/" in name or "\\" in name:
                raise ValueError("must produce one directory name")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ConfigError("paths.meeting_session_name 格式錯誤（可用 {meeting}、{date:%Y%m%d_%H%M%S}，不可分層）") from None
        return safe_name(name)

    @property
    def course_name(self):
        return self.data["course"]["name"]

    def whisper_model_path(self):
        name = self.data["whisper"]["model"]
        m = self.data["whisper"].get("models", {}).get(name)
        return self.path(m["path"]) if m else self.path(name)

    def llm_model(self, name=None):
        name = name or self.data["summary"]["model"]
        m = self.data.get("models", {}).get(name)
        if m is None:
            raise ConfigError(f"未定義的模型：{name}（可用：{', '.join(self.data.get('models', {}))}）")
        return {"name": name, "path": self.path(m["path"]),
                "disable_thinking": bool(m.get("disable_thinking", False))}

    def upstream_inventory(self):
        return {"selected": self.get("summary.upstream", "local"),
                "options": [{"id": "local", "name": "本地 GPU", "kind": "local"}] + [
                    {"id": key, "name": spec["name"], "kind": "api"}
                    for key, spec in load_upstreams().items()]}

    def remote_summary(self):
        key = self.get("summary.upstream", "local")
        if key == "local":
            return None
        spec = load_upstreams().get(key)
        if spec is None:
            raise ConfigError("找不到所選摘要上游；請檢查 config/upstreams.toml 或選擇 local")
        token = spec.get("api_key", "")
        if "api_key_env" in spec:
            token = os.environ.get(spec["api_key_env"], "")
            if not token:
                raise ConfigError("摘要上游的認證環境變數尚未設定")
        if any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise ConfigError("摘要上游認證值格式不合法")
        return spec["base_url"].rstrip("/"), {
            "name": key, "remote": True, "model_id": spec["model"],
            "api_key": token, "disable_thinking": False}

    def capture_plan(self):
        """Validate capture selection without probing or opening any device.

        Dual IDs are canonical Pulse source names, never aliases or list indexes.
        Device existence and monitor role must still be verified by the engine.
        """
        mode = self.get("audio.capture_mode", "single")
        if mode not in ("single", "dual"):
            raise ConfigError("audio.capture_mode 必須是 single 或 dual")
        if mode == "single":
            return {"mode": "single", "tracks": []}
        if self.get("audio.backend", "auto") not in ("auto", "pulse"):
            raise ConfigError("雙音源僅規劃支援 Linux PulseAudio 相容服務")
        aliases = self.get("audio.sources", {})
        if not isinstance(aliases, dict):
            raise ConfigError("audio.sources 必須保留為音源別名表")
        tracks = []
        for role, key in (("system", "system_source"), ("microphone", "microphone_source")):
            value = self.get("audio." + key)
            if (not isinstance(value, str) or not value or value != value.strip()
                    or value in ("default", "auto") or value.isdecimal()
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value)):
                raise ConfigError(f"audio.{key} 必須指定完整 Pulse 來源名稱，不可用預設來源或編號")
            if value in aliases:
                raise ConfigError(f"audio.{key} 不接受 audio.sources 別名；請使用完整來源名稱")
            tracks.append({"id": role, "role": role, "device_id": value})
        if tracks[0]["device_id"] == tracks[1]["device_id"]:
            raise ConfigError("雙音源必須使用不同來源")
        return {"mode": "dual", "tracks": tracks}

    def audio_source(self):
        if self.capture_plan()["mode"] == "dual":
            raise ConfigError("capture_unavailable：雙音源引擎尚未整合，不可降級為單來源")
        src = self.data["audio"]["source"]
        entry = self.data["audio"].get("sources", {}).get(src)
        return entry["pulse"] if entry else src

    def whisper_prompt(self):
        prompt = self.data["whisper"]["prompt"].strip()
        terms = [t for t in self.data["whisper"].get("terms", []) if str(t).strip()]
        if terms:
            prompt += "本課術語：" + "、".join(map(str, terms)) + "。"
        return prompt

    def summary_prompt(self):
        f = self.path(self.data["summary"]["prompt_file"])
        if not f.exists():
            raise ConfigError(f"找不到 prompt 檔：{f}")
        return f.read_text(encoding="utf-8")

    def glossary(self):
        """將 [summary.glossary] 正規化為 term / means / aka 項目。"""
        raw = self.data.get("summary", {}).get("glossary") or {}
        if not isinstance(raw, dict):
            return []
        out = []
        for term, value in raw.items():
            term = str(term).strip()
            if not term:
                continue
            if isinstance(value, dict):
                means = str(value.get("means") or "").strip()
                aliases = value.get("aka") or []
                aka = ([str(alias).strip() for alias in aliases
                        if str(alias).strip()] if isinstance(aliases, list) else [])
            else:
                means, aka = str(value).strip(), []
            aka = [alias for alias in dict.fromkeys(aka) if alias != term]
            out.append({"term": term, "means": means, "aka": aka})
        return out

    def validate(self):
        errs = []
        try:
            self.capture_plan()
        except ConfigError as exc:
            errs.append(str(exc))
        if self.work_type != "meeting":
            upstream = self.get("summary.upstream", "local")
            if not isinstance(upstream, str) or not UPSTREAM_ID.fullmatch(upstream):
                errs.append("summary.upstream 必須是有效的已儲存上游 ID")
            for (sec, key), allowed in CHOICES.items():
                v = self.data.get(sec, {}).get(key)
                if v not in allowed:
                    errs.append(f"{sec}.{key} = {v!r} 不合法（可用：{', '.join(sorted(allowed))}）")
            if upstream == "local" and self.data["summary"]["model"] not in self.data.get("models", {}):
                errs.append(f"summary.model = {self.data['summary']['model']!r} 未在 [models.*] 定義"
                            f"（可用：{', '.join(self.data.get('models', {}))}）")
            if upstream != "local":
                for key in ("request_timeout", "final_wait"):
                    value = self.get("summary." + key)
                    if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                        errs.append(f"summary.{key} 必須是有限正數")
                retries = self.get("summary.retries")
                if type(retries) is not int or retries < 0:
                    errs.append("summary.retries 必須是非負整數")
        if self.data["whisper"]["model"] not in self.data["whisper"].get("models", {}):
            errs.append(f"whisper.model = {self.data['whisper']['model']!r} 未在 [whisper.models.*] 定義")
        if self.work_type == "meeting":
            try:
                self.diarization_options()
                meeting_output_parts(self.get("paths.meeting_output_root"))
                self.meeting_directory_name(datetime.now())
                if not isinstance(self.meeting_name, str) or not self.meeting_name.strip():
                    raise ConfigError("meeting.name 必須是非空字串")
            except ConfigError as exc:
                errs.append(str(exc))
        else:
            glossary = self.data.get("summary", {}).get("glossary", {})
            ignored = self.data.get("summary", {}).get("ignored_terms", [])
            if not isinstance(ignored, list) or not all(isinstance(term, str) and term.strip() for term in ignored):
                errs.append("summary.ignored_terms 應為非空字串陣列")
            if not isinstance(glossary, dict):
                errs.append("summary.glossary 應為 TOML 表格")
            else:
                for term, value in glossary.items():
                    valid = isinstance(value, str)
                    if isinstance(value, dict):
                        means = value.get("means", "")
                        aka = value.get("aka", [])
                        valid = (isinstance(means, str) and isinstance(aka, list)
                                 and all(isinstance(alias, str) for alias in aka))
                    if not valid:
                        errs.append(
                            f'summary.glossary.{term} 應為字串或 '
                            '{ means = "…", aka = ["…"] }'
                        )
        if len(self.whisper_prompt()) > 200:
            self.warnings.append("whisper prompt＋術語超過 200 字，可能超出 whisper 的 prompt 上限而被截斷")
        return errs

    def dump(self):
        data = copy.deepcopy(self.data)
        work = data.get("work")
        data["work"] = {**(work if isinstance(work, dict) else {}), "type": self.work_type}
        return dump_toml(data, f"lec 實際使用的設定（{datetime.now():%Y-%m-%d %H:%M:%S}）")


def load(course_arg=None, sets=(), model=None, source=None, create_missing=False,
         course_file=None):
    """載入並合併設定。

    course_arg: 課名或 .toml 路徑；None 表示只用預設。
    course_file: 直接指定課程設定檔（例如輸出資料夾裡的 config.used.toml）。
    回傳 (Config, created: bool)
    """
    default = load_toml(DEFAULT_FILE)
    data = copy.deepcopy(default)
    warnings, created, cfile, name = [], False, None, None
    if LOCAL_FILE.exists():
        over = load_toml(LOCAL_FILE)
        warnings += [f"local.toml：未知的設定項目 {k}（拼錯了嗎？）" for k in _unknown_keys(default, over)]
        data = deep_merge(data, over)

    if course_file is not None:
        cfile, name = Path(course_file), None
    elif course_arg:
        cfile, name = course_path(course_arg)
        if not cfile.exists():
            if create_missing:
                create_course_file(name, cfile)
                created = True
            else:
                warnings.append(f"找不到課程設定檔 {cfile}，使用預設設定")
                cfile = None

    if cfile is not None and cfile.exists():
        over = load_toml(cfile)
        if cfile.name != "config.used.toml":
            warnings += [f"{cfile.name}：未知的設定項目 {k}（拼錯了嗎？）"
                         for k in _unknown_keys(default, over)]
        data = deep_merge(data, over)

    for expr in sets:
        over = parse_set(expr)
        warnings += [f"--set：未知的設定項目 {k}" for k in _unknown_keys(default, over)]
        data = deep_merge(data, over)
    if model:
        data["summary"]["model"] = model
    if source:
        data["audio"]["source"] = source

    if not data["course"].get("name"):
        data["course"]["name"] = name or "未命名課程"
    cfg = Config(data, cfile, warnings)
    errs = cfg.validate()
    if errs:
        raise ConfigError("設定錯誤：\n  " + "\n  ".join(errs))
    return cfg, created


def load_meeting(meeting_id=None, *, sets=(), source=None):
    """default -> local -> meetings/<id>.toml -> CLI; never loads courses."""
    default = load_toml(DEFAULT_FILE)
    data = copy.deepcopy(default)
    warnings = []
    if LOCAL_FILE.exists():
        over = load_toml(LOCAL_FILE)
        warnings += [f"local.toml：未知的設定項目 {k}" for k in _unknown_keys(default, over)]
        data = deep_merge(data, over)
    path = meeting_path(meeting_id) if meeting_id is not None else None
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"找不到會議設定：{meeting_id}")
        over = load_toml(path)
        if set(over) - MEETING_SECTIONS:
            raise ConfigError("會議設定只允許 meeting、whisper、audio、vad、transcript、diarization")
        unknown = _unknown_keys(default, over)
        if unknown:
            raise ConfigError("會議設定含未知鍵：" + ", ".join(unknown))
        data = deep_merge(data, over)
    for expr in sets:
        over = parse_set(expr)
        if set(over) & {"course", "work"}:
            raise ConfigError("會議覆寫不可改變 course 或 work 類型")
        unknown = _unknown_keys(default, over)
        if unknown:
            raise ConfigError("會議覆寫含未知鍵：" + ", ".join(unknown))
        data = deep_merge(data, over)
    # Reject malformed table/scalar shapes before accessing nested settings.
    for section in MEETING_SECTIONS | {"paths", "system"}:
        if not isinstance(data.get(section), dict):
            raise ConfigError(f"{section} 必須是 TOML 表格")
    name = data["meeting"].get("name")
    if not isinstance(name, str):
        raise ConfigError("meeting.name 必須是字串")
    data["meeting"]["name"] = name.strip() or meeting_id or "未命名會議"
    data["work"] = {"type": "meeting"}
    # Summary is irrelevant even if local/course defaults point at absent models.
    summary = data.get("summary")
    data["summary"] = {**(summary if isinstance(summary, dict) else {}), "enabled": False}
    if source is not None:
        data["audio"]["source"] = source
    cfg = Config(data, path, warnings)
    try:
        errors = cfg.validate()
    except (TypeError, KeyError, AttributeError, ValueError):
        raise ConfigError("會議設定欄位型別錯誤，請檢查 whisper/audio/vad/transcript 設定") from None
    if errors:
        raise ConfigError("設定錯誤：\n  " + "\n  ".join(errors))
    return cfg
