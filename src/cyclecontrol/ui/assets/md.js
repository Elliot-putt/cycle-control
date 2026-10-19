(function (root) {
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const BARE_URL = /(^|[\s(>])(https:\/\/[^\s<>"')\]]+?)([.,;:!?]*)(?=$|[\s)<])/g;

  function inline(text) {
    const codes = [];
    let out = String(text ?? "").replace(/`([^`\n]+)`/g, (_, code) => {
      codes.push(code);
      return `\u0000${codes.length - 1}\u0000`;
    });
    out = esc(out)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\s][^*\n]*?)\*(?![*\w])/g, "$1<em>$2</em>")
      .replace(/\[([^\]\n]+)\]\((https:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(BARE_URL, (_, lead, url, tail) => `${lead}<a href="${url}" target="_blank" rel="noopener">${url}</a>${tail}`);
    return out.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[Number(i)])}</code>`);
  }

  const isTableRow = (line) => /^\s*\|.*\|\s*$/.test(line);
  const isSeparator = (line) => /^\s*\|[\s:|-]+\|\s*$/.test(line) && line.includes("-");
  const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

  function render(text) {
    const lines = String(text ?? "").replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let paragraph = [];
    const flush = () => {
      if (paragraph.length) html.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
      paragraph = [];
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (/^\s*```/.test(line)) {
        flush();
        const code = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) code.push(lines[i]);
        html.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
        continue;
      }
      if (isTableRow(line) && i + 1 < lines.length && isSeparator(lines[i + 1])) {
        flush();
        const head = cells(line);
        const rows = [];
        for (i += 2; i < lines.length && isTableRow(lines[i]); i++) rows.push(cells(lines[i]));
        i--;
        html.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flush();
        html.push(`<h4>${inline(heading[2])}</h4>`);
        continue;
      }
      const item = line.match(/^\s*([-*]|\d+[.)])\s+(.*)$/);
      if (item) {
        flush();
        const ordered = /\d/.test(item[1]);
        const items = [];
        for (; i < lines.length; i++) {
          const m = lines[i].match(/^\s*([-*]|\d+[.)])\s+(.*)$/);
          if (!m || /\d/.test(m[1]) !== ordered) break;
          items.push(m[2]);
        }
        i--;
        const tag = ordered ? "ol" : "ul";
        html.push(`<${tag}>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</${tag}>`);
        continue;
      }
      if (!line.trim()) {
        flush();
        continue;
      }
      paragraph.push(line);
    }
    flush();
    return html.join("");
  }

  root.renderMarkdown = render;
  if (typeof module !== "undefined") module.exports = { render };
})(typeof window !== "undefined" ? window : globalThis);
