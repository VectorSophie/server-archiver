const form = document.getElementById("search-form");
const input = document.getElementById("query");
const results = document.getElementById("results");
const meta = document.getElementById("meta");
const operatorName = document.getElementById("operator-name");

const HINT_HTML = results.innerHTML;

fetch("/api/whoami")
  .then((r) => r.json())
  .then((data) => {
    if (!data.codename) {
      window.location.href = "/login";
      return;
    }
    operatorName.textContent = data.codename.toUpperCase();
  });

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderResults(data) {
  if (data.error) {
    results.innerHTML = `<div class="empty">error: ${escapeHtml(data.error)}</div>`;
    meta.style.display = "none";
    return;
  }
  if (data.count === 0) {
    results.innerHTML = `<div class="empty">no matches. try a from: or in: filter.</div>`;
    meta.style.display = "none";
    return;
  }
  meta.style.display = "block";
  meta.textContent = `${data.count} matches`;
  results.innerHTML = data.results.map((row) => `
    <div class="row">
      <div class="head">
        <span class="time">${escapeHtml(row.time)}</span>
        <span class="channel">#${escapeHtml(row.channel)}</span>
        <span class="author">${escapeHtml(row.author)}</span>
      </div>
      <div class="content">${escapeHtml(row.content)}</div>
    </div>
  `).join("");
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = input.value.trim();
  if (!query) {
    results.innerHTML = HINT_HTML;
    meta.style.display = "none";
    return;
  }
  const resp = await fetch("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (resp.status === 401) {
    window.location.href = "/login";
    return;
  }
  renderResults(await resp.json());
});
