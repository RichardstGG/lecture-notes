import { useEffect, useRef, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { api } from "../api";
import type { SharingStatus } from "../types";

export function SharePanel({ selectedId, workType = "lecture" }: { selectedId?: string; workType?: "lecture" | "meeting" }) {
  const [status, setStatus] = useState<SharingStatus>();
  const [host, setHost] = useState("");
  const [allInterfaces, setAllInterfaces] = useState(true);
  const [networkHint, setNetworkHint] = useState("正在偵測預設路由介面…");
  const [port, setPort] = useState("8766");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [copied, setCopied] = useState(false);
  const revision = useRef(0);
  const acting = useRef(false);
  const pollError = useRef(false);
  const sharedType = status?.active ? (status.work_type || workType) : workType;

  useEffect(() => {
    let disposed = false;
    void api.sharingNetwork().then((value) => {
      if (disposed) return;
      setHost((current) => current || value.advertise_host || "");
      setNetworkHint(value.advertise_host
        ? `預設路由介面：${value.interface} · ${value.advertise_host}`
        : value.error || "請手動輸入本機可連線的 IPv4。");
    }).catch(() => {
      if (!disposed) setNetworkHint("無法偵測預設路由，請手動輸入本機可連線的 IPv4。");
    });
    return () => { disposed = true; };
  }, []);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      const current = revision.current;
      try {
        if (!acting.current) {
          const value = await api.sharing();
          if (!disposed && current === revision.current) {
            setStatus(value);
            if (value.error) setError(value.error);
            else if (pollError.current) setError(undefined);
            pollError.current = false;
          }
        }
      } catch (reason) {
        if (!disposed && current === revision.current) {
          pollError.current = true;
          setError(reason instanceof Error ? reason.message : "無法讀取分享狀態");
        }
      } finally {
        if (!disposed) timer = setTimeout(refresh, 2000);
      }
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timer); };
  }, []);

  async function change(open: boolean) {
    acting.current = true;
    revision.current++;
    setBusy(true);
    setError(undefined);
    setCopied(false);
    try {
      const value = open
        ? await api.openSharing(selectedId!, allInterfaces ? "0.0.0.0" : host.trim(), Number(port),
          allInterfaces ? host.trim() : undefined)
        : await api.closeSharing();
      setStatus(value);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "分享操作失敗");
    } finally {
      acting.current = false;
      setBusy(false);
    }
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(status!.url!);
      setCopied(true);
    } catch {
      setError("無法自動複製，請選取下方連結手動複製。");
    }
  }

  return <section className="share-card" aria-label="內網唯讀分享">
    <div className="card-title"><div><p className="eyebrow">LAN SHARING</p><h2>內網唯讀分享</h2></div>
    </div>
    <p>固定分享一場{sharedType === "meeting" ? "原逐字稿與帶發言者逐字稿" : "逐字稿與筆記"}，最多 20 位在線。停止錄音後仍可閱讀與下載，直到手動關閉分享或主控服務結束。</p>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {status?.active ? <>
      <p><strong>分享場次：</strong>{status.session_id}（不隨目前檢視場次切換）</p>
      <p>監聽位址：{status.bind_host || status.advertise_host || "指定內網 IP"}</p>
      {status.url && <figure className="share-qr">
        <QRCodeSVG value={status.url} size={224} marginSize={4} level="M" role="img" aria-label="參與者連結 QR code" />
        <figcaption>掃描加入這場分享（需與主機網路互通）</figcaption>
      </figure>}
      <label>參與者連結<input readOnly value={status.url || ""} onFocus={(event) => event.target.select()} /></label>
      <div className="share-actions">
        <button className="button secondary" onClick={() => void copyLink()}>{copied ? "已複製" : "複製連結"}</button>
        <button className="button share-close" disabled={busy} onClick={() => void change(false)}>關閉分享</button>
      </div>
      <p>在線 {status.participants.filter((person) => person.online).length} / 20 · 約 30 秒未收到更新即顯示離線</p>
      <ul className="share-roster">{status.participants.map((person) => <li key={person.id}>
        <span>{person.nickname}</span><span>{person.online ? "在線" : "離線"}</span>
      </li>)}</ul>
    </> : <form className="share-form" onSubmit={(event) => { event.preventDefault(); void change(true); }}>
      <p>將分享目前選定場次：{selectedId || "請先選擇場次"}</p>
      <label className="share-listen-option"><input type="checkbox" checked={allInterfaces} onChange={(event) => setAllInterfaces(event.target.checked)} />監聽所有 IPv4 介面（0.0.0.0）</label>
      <small>{networkHint}</small>
      <label>連結使用的本機 IPv4<input placeholder="例如 192.168.1.10" value={host} onChange={(event) => setHost(event.target.value)} required /></label>
      <label>分享埠<input type="number" min="1024" max="65535" value={port} onChange={(event) => setPort(event.target.value)} required /></label>
      <button className="button primary" disabled={busy || !selectedId || !status}>{busy ? "啟動中…" : "開啟分享"}</button>
      <small>連結與 QR code 使用上方 IP。選取所有介面時，分享埠會開放於所有 IPv4 網路介面；主控仍只在 127.0.0.1。</small>
    </form>}
  </section>;
}
