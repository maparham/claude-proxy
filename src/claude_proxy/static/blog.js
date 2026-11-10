// A blog post's table of contents: it marks the section being read, and starts folded on a narrow screen, where it
// sits above the post instead of beside it.
const toc = document.querySelector(".toc");
if (toc) {
  // folded while narrow, open again once wide (the wide layout's summary takes no clicks, so it must not stay folded)
  const narrow = matchMedia("(max-width: 959px)");
  const fold = () => { toc.querySelector("details").open = !narrow.matches; };
  narrow.addEventListener("change", fold);
  fold();
  const links = new Map([...toc.querySelectorAll("a")].map(a => [a.hash.slice(1), a]));
  const heads = [...links.keys()].map(id => document.getElementById(id));
  const mark = () => {
    // the last heading above the top fifth of the window is the section being read
    let cur = heads[0];
    for (const h of heads) if (h.getBoundingClientRect().top < innerHeight / 5) cur = h;
    links.forEach((a, id) => a.classList.toggle("on", id === cur.id));
  };
  addEventListener("scroll", mark, { passive: true });
  mark();
}

// Every code block in a post gets a copy button, so a command or a prompt to paste into gclaude is one click away.
for (const pre of document.querySelectorAll(".post pre")) {
  const wrap = document.createElement("div");
  wrap.className = "copy-wrap";
  pre.replaceWith(wrap);
  wrap.append(pre);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn small copy";
  btn.textContent = "کپی";
  btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(pre.innerText.trim()); btn.textContent = "کپی شد"; }
    catch { btn.textContent = "کپی نشد"; }
    setTimeout(() => { btn.textContent = "کپی"; }, 1500);
  });
  wrap.append(btn);
}
