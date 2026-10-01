const form = document.getElementById("search-form");
const input = document.getElementById("query");
const results = document.getElementById("results");
const stats = document.getElementById("stats");
const spinner = document.getElementById("spinner");
const cursor = document.getElementById("cursor");
const downloadBtn = document.getElementById("download-btn");
const operatorName = document.getElementById("operator-name");

const HINT_HTML = results.innerHTML;
let lastQuery = "";

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

function renderStats(queryStats) {
  if (!queryStats || queryStats.count === 0) {
    stats.style.display = "none";
    return;
  }
  stats.style.display = "flex";
  document.getElementById("stat-count").textContent = queryStats.count;
  document.getElementById("stat-span").textContent = queryStats.date_span;
  document.getElementById("stat-freq").textContent = queryStats.frequency;
  document.getElementById("stat-words").textContent = queryStats.top_words.length
    ? queryStats.top_words.map(([word, count]) => `${word} (${count})`).join(", ")
    : "--";
}

function renderResults(data) {
  if (data.error) {
    results.innerHTML = `<div class="empty">error: ${escapeHtml(data.error)}</div>`;
    stats.style.display = "none";
    downloadBtn.disabled = true;
    return;
  }
  renderStats(data.stats);
  downloadBtn.disabled = data.count === 0;
  if (data.count === 0) {
    results.innerHTML = `<div class="empty">no matches. try a from: or in: filter.</div>`;
    return;
  }
  results.innerHTML = data.results.map((row, i) => `
    <div class="row ${i % 2 === 1 ? "row-r" : ""}">
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
    stats.style.display = "none";
    downloadBtn.disabled = true;
    return;
  }
  lastQuery = query;
  spinner.hidden = false;
  cursor.hidden = true;
  const resp = await fetch("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  spinner.hidden = true;
  cursor.hidden = false;
  if (resp.status === 401) {
    window.location.href = "/login";
    return;
  }
  renderResults(await resp.json());
});

downloadBtn.addEventListener("click", () => {
  if (downloadBtn.disabled || !lastQuery) return;
  window.location.href = `/api/export?query=${encodeURIComponent(lastQuery)}&format=txt`;
});

// A small cycling typewriter status line -- purely decorative, no real data.
const TYPEWRITER_PHRASES = [
  "archive breathing",
  "879 files tracked",
  "sync: 0 changed",
  "snapshot verified",
];

function runTypewriter(el, phrases) {
  let phraseIndex = 0;
  let charIndex = 0;
  let deleting = false;

  function tick() {
    const phrase = phrases[phraseIndex];
    el.textContent = deleting ? phrase.slice(0, charIndex) : phrase.slice(0, charIndex);
    if (!deleting) {
      charIndex++;
      if (charIndex > phrase.length) {
        deleting = true;
        setTimeout(tick, 1400);
        return;
      }
    } else {
      charIndex--;
      if (charIndex < 0) {
        deleting = false;
        charIndex = 0;
        phraseIndex = (phraseIndex + 1) % phrases.length;
      }
    }
    setTimeout(tick, deleting ? 30 : 70);
  }
  tick();
}

const typewriterEl = document.getElementById("typewriter");
if (typewriterEl) {
  runTypewriter(typewriterEl, TYPEWRITER_PHRASES);
}
