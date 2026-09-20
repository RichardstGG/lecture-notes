function renderLine(line: string, index: number) {
  if (line.startsWith("### ")) return <h4 key={index}>{line.slice(4)}</h4>;
  if (line.startsWith("## ")) return <h3 key={index}>{line.slice(3)}</h3>;
  if (line.startsWith("# ")) return <h2 key={index}>{line.slice(2)}</h2>;
  if (line.startsWith("- ")) return <div className="markdown-bullet" key={index}><span>•</span><p>{line.slice(2)}</p></div>;
  if (/^\d+\. /.test(line)) return <div className="markdown-bullet" key={index}><span>{line.match(/^\d+/)?.[0]}.</span><p>{line.replace(/^\d+\. /, "")}</p></div>;
  if (!line.trim()) return <div className="markdown-space" key={index} />;
  return <p key={index}>{line}</p>;
}

export function MarkdownPane({ content, empty }: { content?: string; empty: string }) {
  if (!content?.trim()) return <div className="empty-copy">{empty}</div>;
  return <div className="markdown-content">{content.split("\n").map(renderLine)}</div>;
}
