"""第 2 步：把 transcript.md 依 ## 時間小標題分段，送 llama-server 總結，輸出 notes.md。

- notes.jsonl 是總結結果的正式紀錄（也是續跑依據）；notes.md 由它排版而來。
- 一個小節「後面出現下一個小標題」才算完成；結束時（done）才處理最後一節。
- 少於 summary.min_chars 字的小節會併入下一節。
- LLM 以 JSON schema 回覆，程式驗證「老師強調」原句與術語確實出現在逐字稿中。
"""
import json
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .transcribe import parse_srt
from .util import atomic_write, hms, log, opencc_available, opencc_convert, parse_hms

HEADER_RE = re.compile(r"^## (\d{2}:\d{2}:\d{2})[ \t]*$", re.M)
STAMP_RE = re.compile(r"`(\d{2}:\d{2}:\d{2})`")
THINK_RE = re.compile(r"(?s)<think>.*?</think>")

SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "terms": {
            "type": "array", "maxItems": 8,
            "items": {"type": "object",
                      "properties": {"term": {"type": "string"},
                                     "explain": {"type": "string"},
                                     "asr_original": {"type": "string"}},
                      "required": ["term", "explain", "asr_original"]},
        },
        "emphasis": {
            "type": "array", "maxItems": 5,
            "items": {"type": "object",
                      "properties": {"quote": {"type": "string"},
                                     "note": {"type": "string"}},
                      "required": ["quote", "note"]},
        },
    },
    "required": ["topic", "points", "terms", "emphasis"],
}


@dataclass
class Section:
    label: str
    start: float
    body: str

    @property
    def chars(self):
        return len(re.sub(r"\s", "", STAMP_RE.sub("", self.body)))


def parse_sections(text):
    ms = list(HEADER_RE.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out.append(Section(m.group(1), parse_hms(m.group(1)), text[m.end():end].strip()))
    return out


def link_label(label):
    # Obsidian 標題連結不支援「:」，會以空白代替
    return label.replace(":", " ")


# ---------------------------------------------------------------- 驗證
def _normalize(text):
    """只留文字與數字（小寫），並記錄每個字在原文的位置。"""
    chars, idx = [], []
    for i, ch in enumerate(text):
        if ch.isalnum():
            chars.append(ch.lower())
            idx.append(i)
    return "".join(chars), idx


def _bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def find_quote(quote, norm_text):
    """在正規化後的逐字稿中找最接近 quote 的片段，回傳 (相似度, 起點, 長度)。"""
    q, _ = _normalize(quote)
    m, n = len(q), len(norm_text)
    if m == 0 or n == 0:
        return 0.0, 0, 0
    pos = norm_text.find(q)
    if pos >= 0:
        return 1.0, pos, m
    if m >= n:
        return SequenceMatcher(None, q, norm_text, autojunk=False).ratio(), 0, n
    qb = _bigrams(q)
    step = max(1, m // 6)
    best, best_i = -1.0, 0
    for i in range(0, n - m + 1, step):
        w = norm_text[i:i + m]
        score = len(qb & _bigrams(w)) / len(qb)
        if score > best:
            best, best_i = score, i
    top = (0.0, best_i, m)
    for i in range(max(0, best_i - step), min(n - 1, best_i + step) + 1):
        for L in (m - m // 10, m, m + m // 10):
            L = max(1, min(L, n - i))
            r = SequenceMatcher(None, q, norm_text[i:i + L], autojunk=False).ratio()
            if r > top[0]:
                top = (r, i, L)
    return top


# ---------------------------------------------------------------- 主類別
class Summarizer:
    def __init__(self, cfg, session_dir, llm_url, model, status=None):
        self.cfg = cfg
        self.s = cfg["summary"]
        self.session = Path(session_dir)
        self.url = llm_url.rstrip("/")
        self.model = model                      # {"name","path","disable_thinking"}
        self.status = status
        self.transcript = self.session / "transcript.md"
        self.srt = self.session / "transcript.srt"
        self.notes_md = self.session / "notes.md"
        self.jsonl = self.session / "notes.jsonl"
        self.use_schema = True
        self.warmed = False
        self.abort = threading.Event()
        self.system_prompt = self._build_system_prompt()
        self.entries = self._load_entries()
        self.use_opencc = bool(cfg["system"]["opencc"] and opencc_available())

    # -- prompt
    def _glossary_prompt(self):
        entries = self.cfg.glossary()
        if not entries:
            return ""
        lines = [
            "# 本課程術語對照表",
            "- 左邊是正式寫法，「可能被聽成」是老師發音不準或語音辨識常出現的錯誤寫法。",
            "- 本段逐字稿出現任一錯誤寫法時，一律視為該正式術語：topic 與 points 使用正式寫法，terms 的 term 填正式寫法、asr_original 填逐字稿中原本的寫法。",
            "- 「是什麼」只用來幫你認出這個詞，不可寫進 explain、points 或任何欄位；explain 仍然只能寫老師在逐字稿裡講過的內容。",
            "- 對照表裡沒有在本段逐字稿出現的詞，不可寫進筆記。",
        ]
        for entry in entries:
            parts = [entry["term"]]
            if entry["aka"]:
                parts.append("可能被聽成：" + "、".join(entry["aka"]))
            if entry["means"]:
                parts.append("是什麼：" + entry["means"])
            lines.append("- " + "｜".join(parts))
        return "\n".join(lines) + "\n"

    def _build_system_prompt(self):
        tpl = self.cfg.summary_prompt()
        terms = [str(t) for t in self.cfg["whisper"].get("terms", []) if str(t).strip()]
        terms_txt = ("- 本課程常用術語（只作為判斷錯字的參考，不代表本段有提到，不可因此加入筆記）："
                     + "、".join(terms) + "\n") if terms else ""
        glossary_txt = self._glossary_prompt()
        if not glossary_txt:
            # 預設 prompt 把 placeholder 獨立放一行；空表時連同行尾移除，
            # 讓產生的 prompt 與加入 glossary 功能前逐字相同。
            tpl = tpl.replace("{glossary}\n", "").replace("{glossary}", "")
        extra = self.s.get("extra_instructions", "").strip()
        extra_txt = f"\n# 本課程補充說明\n{extra}\n" if extra else ""
        for key, val in (("{course}", self.cfg.course_name), ("{terms}", terms_txt),
                         ("{glossary}", glossary_txt),
                         ("{extra_instructions}", extra_txt)):
            if key in tpl:
                tpl = tpl.replace(key, val)
            elif val and key != "{course}":
                tpl += "\n" + val
        return tpl.strip() + "\n"

    # -- 紀錄
    def _load_entries(self):
        entries = {}
        if self.jsonl.exists():
            for line in self.jsonl.read_text(encoding="utf-8").splitlines():
                try:
                    e = json.loads(line)
                    entries[e["label"]] = e            # 同一段重做時，以最後一筆為準
                except (ValueError, KeyError):
                    pass
        return entries

    def covered(self):
        return {lab for e in self.entries.values() for lab in e.get("sections", [e["label"]])}

    def _ordered(self):
        return sorted(self.entries.values(), key=lambda e: e["start"])

    # -- 分段
    def read_sections(self):
        try:
            return parse_sections(self.transcript.read_text(encoding="utf-8"))
        except OSError:
            return []

    def next_block(self, sections, done):
        covered = self.covered()
        pending = [s for s in sections if s.label not in covered]
        if not done and pending and sections and pending[-1] is sections[-1]:
            pending = pending[:-1]              # 最後一節還在寫
        block = []
        for s in pending:
            block.append(s)
            if sum(x.chars for x in block) >= self.s["min_chars"]:
                return block
        return block if (done and block) else None

    def _time_range(self, block, sections):
        start = block[0].start
        i = sections.index(block[-1])
        next_start = sections[i + 1].start if i + 1 < len(sections) else float("inf")
        end = None
        try:
            cues = parse_srt(self.srt.read_text(encoding="utf-8"))
            ends = [b for a, b, _ in cues if start - 1 <= a < next_start]
            end = max(ends) if ends else None
        except OSError:
            pass
        if end is None:
            stamps = [parse_hms(t) for t in STAMP_RE.findall(block[-1].body)]
            end = max(stamps) if stamps else start
            if next_start != float("inf"):
                end = max(end, min(next_start, end + 30))
        return start, end

    @staticmethod
    def _prep_body(block):
        text = "\n\n".join(s.body for s in block)
        text = re.sub(r"[，、]?…+[，、]?", "…", text)
        text = re.sub(r"(…\s*){2,}", "…", text)
        return text

    # -- LLM
    def _post(self, body, timeout):
        req = urllib.request.Request(f"{self.url}/v1/chat/completions",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def _request_body(self, messages, max_tokens):
        body = {"messages": messages, "temperature": self.s["temperature"],
                "top_p": self.s["top_p"], "max_tokens": max_tokens, "cache_prompt": True}
        if self.model["disable_thinking"]:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    def warmup(self):
        """第一次呼叫含 shader 編譯，先暖機避免第一段特別慢。"""
        if self.warmed:
            return
        self.warmed = True
        suffix = " /no_think" if self.model["disable_thinking"] else ""
        try:
            self._post(self._request_body([{"role": "user", "content": "你好" + suffix}], 1), 120)
        except Exception:
            pass

    def call_llm(self, user_msg):
        messages = [{"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_msg}]
        attempts = self.s["retries"] + 1
        last_err = None
        max_tokens = self.s["max_tokens"]
        i = 0
        while i < attempts and not self.abort.is_set():
            body = self._request_body(messages, max_tokens)
            if self.use_schema:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "lecture_notes", "schema": SCHEMA}}
            r = None
            try:
                r = self._post(body, self.s["request_timeout"])
                content = r["choices"][0]["message"].get("content") or ""
                data = self._parse_json(content)
                return data, r
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                if e.code == 400 and self.use_schema and ("response_format" in msg or "schema" in msg
                                                          or "grammar" in msg):
                    log("  ⚠ llama-server 不支援 JSON schema，改用一般輸出再解析")
                    self.use_schema = False
                    continue
                last_err = f"HTTP {e.code}: {msg}"
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                last_err = f"連線失敗：{e}"
            except (ValueError, KeyError, IndexError) as e:
                last_err = f"回覆無法解析：{e}"
                try:
                    truncated = r["choices"][0].get("finish_reason") == "length"
                except (TypeError, KeyError, IndexError):
                    truncated = False
                if truncated:
                    # JSON 寫到一半就達到 max_tokens：放寬上限再試
                    last_err = f"輸出達到 max_tokens={max_tokens} 被截斷"
                    max_tokens = int(max_tokens * 1.5)
            i += 1
            if self.abort.is_set():
                break
            if i < attempts:
                log(f"  ⚠ 總結請求失敗（第 {i} 次）：{last_err}")
                time.sleep(3)
        raise RuntimeError(last_err or "已中止")

    @staticmethod
    def _parse_json(content):
        content = THINK_RE.sub("", content).strip()
        try:
            data = json.loads(content)
        except ValueError:
            m = re.search(r"(?s)\{.*\}", content)
            if not m:
                raise ValueError("回覆中找不到 JSON")
            data = json.loads(m.group(0))
        if not isinstance(data, dict):
            raise ValueError("回覆不是 JSON 物件")

        def strs(v):
            return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

        def objs(v, keys):
            out = []
            for x in v if isinstance(v, list) else []:
                if isinstance(x, dict):
                    out.append({k: str(x.get(k) or "").strip() for k in keys})
            return out

        return {"topic": str(data.get("topic") or "").strip(),
                "points": strs(data.get("points")),
                "terms": [t for t in objs(data.get("terms"), ("term", "explain", "asr_original"))
                          if t["term"]],
                "emphasis": [e for e in objs(data.get("emphasis"), ("quote", "note")) if e["quote"]]}

    # -- 後處理
    def _convert(self, data):
        if not self.use_opencc:
            return data
        refs, texts = [], []

        def add(obj, key):
            refs.append((obj, key))
            texts.append(re.sub(r"\s*\n\s*", " ", obj[key]))

        add(data, "topic")
        for i in range(len(data["points"])):
            refs.append((data["points"], i))
            texts.append(re.sub(r"\s*\n\s*", " ", data["points"][i]))
        for t in data["terms"]:
            add(t, "explain")
        for e in data["emphasis"]:
            add(e, "note")
        # term / asr_original / quote 不轉換（要跟逐字稿比對）
        for (obj, key), val in zip(refs, opencc_convert(texts)):
            obj[key] = val
        return data

    def _verify(self, data, body, label):
        norm, idx = _normalize(STAMP_RE.sub(lambda m: " " * len(m.group()), body))
        stamps = [(m.start(), m.group(1)) for m in STAMP_RE.finditer(body)]
        report = {"emphasis_dropped": [], "terms_unverified": []}

        kept = []
        for e in data["emphasis"]:
            score, pos, length = find_quote(e["quote"], norm)
            if score < self.s["quote_match"] or not idx:
                report["emphasis_dropped"].append(e["quote"])
                continue
            a = idx[pos]
            b = idx[min(pos + length, len(idx)) - 1] + 1
            time_label = label
            for off, st in stamps:
                if off <= a:
                    time_label = st
            e["quote"] = body[a:b].strip()          # 換成逐字稿原文
            e["time"] = time_label
            e["match"] = round(score, 2)
            if all(k["quote"] != e["quote"] for k in kept):
                kept.append(e)
        data["emphasis"] = kept

        mode = self.s["unverified_terms"]
        terms, seen = [], set()
        for t in data["terms"]:
            key = _normalize(t["term"])[0]
            if not key or key in seen:
                continue
            seen.add(key)
            if t["asr_original"] and _normalize(t["asr_original"])[0] == key:
                t["asr_original"] = ""
            found = key in norm or (t["asr_original"] and _normalize(t["asr_original"])[0] in norm)
            t["verified"] = bool(found)
            if not found:
                report["terms_unverified"].append(t["term"])
                if mode == "drop":
                    continue
            terms.append(t)
        data["terms"] = terms
        return report

    # -- 輸出
    def _ensure_header(self):
        if self.notes_md.exists() and self.notes_md.stat().st_size:
            return
        date = datetime.now().strftime("%Y-%m-%d")
        try:
            m = re.search(r"^date:\s*(\S+)", self.transcript.read_text(encoding="utf-8"), re.M)
            if m:
                date = m.group(1)
        except OSError:
            pass
        course = self.cfg.course_name
        self.notes_md.write_text(f"---\ncourse: {course}\ndate: {date}\ntype: notes\n"
                                 f"tags: [課堂筆記]\n---\n\n# {course} 課堂筆記 {date}\n",
                                 encoding="utf-8")

    def render(self, e):
        label = e["label"]
        lines = ["", f"## {label} – {hms(e['end'])}",
                 f"> 逐字稿：[[transcript#{link_label(label)}|{label}]]", ""]
        if e["status"] == "empty":
            return "\n".join(lines + ["（本段無內容）", ""])
        if e["status"] == "failed":
            return "\n".join(lines + [
                f"> ⚠ 此段總結失敗：{e.get('error', '')}",
                f"> 可執行：`lec summarize \"{self.session}\" --redo {label}`", ""])
        mode = self.s["unverified_terms"]
        lines += ["### 主題", e.get("topic") or "（無）", "", "### 重點"]
        lines += [f"- {p}" for p in e["points"]] or ["（無）"]
        lines += ["", "### 術語"]
        if not e["terms"]:
            lines.append("（無）")
        for t in e["terms"]:
            s = f"**{t['term']}**"
            if t.get("asr_original"):
                s += f"（辨識為：{t['asr_original']}）"
            if t.get("explain"):
                s += f"：{t['explain']}"
            if not t.get("verified", True) and mode == "mark":
                s = f"⚠ {s}（逐字稿中找不到此詞）"
            lines.append(f"- {s}")
        lines += ["", "### 老師強調"]
        lines += [f"- `{x['time']}` {x['note']}：「{x['quote']}」" for x in e["emphasis"]] or ["（無）"]
        return "\n".join(lines + [""])

    def _save(self, e, rebuild=False):
        self.entries[e["label"]] = e
        with open(self.jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        if rebuild:
            self.rebuild_notes()
        else:
            self._ensure_header()
            with open(self.notes_md, "a", encoding="utf-8") as f:
                f.write(self.render(e))
        if self.status:
            self.status.update(sections_summarized=len(self.covered()))
            self.status.event("summary", label=e["label"], status=e["status"],
                              topic=e.get("topic", ""), elapsed=e.get("elapsed"))

    def rebuild_notes(self):
        if self.notes_md.exists():
            shutil.copy2(self.notes_md, self.notes_md.with_suffix(".md.bak"))
            self.notes_md.unlink()
        self._ensure_header()
        with open(self.notes_md, "a", encoding="utf-8") as f:
            for e in self._ordered():
                f.write(self.render(e))

    # -- 處理一個區塊
    def process(self, block, sections, rebuild=False):
        start, end = self._time_range(block, sections)
        label = block[0].label
        chars = sum(s.chars for s in block)
        base = {"label": label, "start": start, "end": end,
                "sections": [s.label for s in block], "chars": chars,
                "model": self.model["name"], "created": datetime.now().isoformat(timespec="seconds")}
        merged = f"（合併 {len(block)} 節）" if len(block) > 1 else ""

        if chars < self.s["empty_chars"]:
            log(f"▷ 總結 {label}–{hms(end)}：內容太少，標記為無內容")
            self._save({**base, "status": "empty"}, rebuild)
            return

        body = self._prep_body(block)
        prev = [e for e in self._ordered() if e["start"] < start and e["status"] == "ok"]
        prev = prev[-self.s["context_sections"]:] if self.s["context_sections"] > 0 else []
        recap = "\n".join(f"- {e['label']} {e['topic']}" for e in prev) or "（這是第一段）"
        user = (f"課程：{self.cfg.course_name}\n時間範圍：{label}–{hms(end)}\n\n"
                f"前情提要（前幾段的主題，只供理解上下文）：\n{recap}\n\n"
                f"本段逐字稿：\n<<<\n{body}\n>>>")
        if self.model["disable_thinking"]:
            user += "\n\n/no_think"

        log(f"▶ 總結 {label}–{hms(end)}{merged}，{chars} 字…")
        if self.status:
            self.status.update(llm_busy=True, llm_section=label)
        t0 = time.time()
        try:
            self.warmup()
            t0 = time.time()
            data, raw = self.call_llm(user)
        except Exception as e:
            log(f"  ✖ 總結 {label} 失敗：{e}")
            if self.status:
                self.status.error(f"總結 {label} 失敗：{e}")
            if not self.abort.is_set():
                self._save({**base, "status": "failed", "error": str(e)[:200]}, rebuild)
            return
        finally:
            if self.status:
                self.status.update(llm_busy=False, llm_section=None)

        elapsed = time.time() - t0
        data = self._convert(data)
        report = self._verify(data, body, label)
        usage = raw.get("usage", {})
        e = {**base, "status": "ok", **data, "verify": report, "elapsed": round(elapsed, 1),
             "prompt_tokens": usage.get("prompt_tokens"),
             "completion_tokens": usage.get("completion_tokens"),
             "finish_reason": raw["choices"][0].get("finish_reason")}
        self._save(e, rebuild)
        notes = []
        if report["emphasis_dropped"]:
            notes.append(f"刪除 {len(report['emphasis_dropped'])} 條找不到原句的強調")
        if report["terms_unverified"]:
            notes.append(f"術語未驗證：{'、'.join(report['terms_unverified'])}")
        if e["finish_reason"] == "length":
            notes.append("輸出達到 max_tokens 上限")
        log(f"✔ 總結 {label} 完成 {elapsed:.1f}s（輸出 {usage.get('completion_tokens')} tokens）"
            f"：{data['topic']}" + (f"｜{'；'.join(notes)}" if notes else ""))

    # -- 執行模式
    def run_live(self, finish_event):
        """背景執行緒：持續監看逐字稿；finish_event 設定後補做最後一段再結束。"""
        while not self.abort.is_set():
            finishing = finish_event.is_set()
            sections = self.read_sections()
            if self.status:
                self.status.update(sections_total=len(sections))
            block = self.next_block(sections, done=finishing)
            if block:
                self.process(block, sections)
                continue
            if finishing:
                break
            finish_event.wait(self.s["poll_seconds"])

    def run_all(self, rebuild=False):
        """逐字稿已完成：處理所有尚未總結的小節。"""
        n = 0
        while not self.abort.is_set():
            sections = self.read_sections()
            if self.status:
                self.status.update(sections_total=len(sections))
            block = self.next_block(sections, done=True)
            if not block:
                break
            self.process(block, sections, rebuild=rebuild)
            n += 1
        return n

    def redo(self, label):
        sections = self.read_sections()
        if label == "all":
            for p in (self.jsonl, self.notes_md):
                if p.exists():
                    shutil.copy2(p, p.with_name(p.name + ".bak"))
                    p.unlink()
            self.entries = {}
            log(f"▶ 重做全部總結（舊檔已備份為 .bak）")
            return self.run_all()
        block = self.redo_block(label, sections)
        self.process(block, sections, rebuild=True)
        return 1

    def redo_block(self, label, sections=None):
        """檢查 --redo 的時間是否可用，回傳要重做的小節。"""
        sections = sections if sections is not None else self.read_sections()
        by_label = {s.label: s for s in sections}
        if label not in by_label:
            raise ValueError(f"逐字稿中沒有小標題 {label}（可用：{', '.join(by_label) or '無'}）")
        old = self.entries.get(label)
        if old:
            return [by_label[l] for l in old.get("sections", [label]) if l in by_label]
        if label in self.covered():
            owner = next(e["label"] for e in self.entries.values() if label in e.get("sections", []))
            raise ValueError(f"{label} 已併入 {owner} 那段的總結，請改用 --redo {owner}")
        return [by_label[label]]
