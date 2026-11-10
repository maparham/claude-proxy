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
  const label = text => {   // a copy icon (two sheets) beside the word, a check once copied
    const icon = text === "کپی شد" ? '<path d="M5 12l5 5 9-10"/>'
      : '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>';
    btn.innerHTML = `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${icon}</svg><span>${text}</span>`;
  };
  label("کپی");
  btn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(pre.innerText.trim()); label("کپی شد"); }
    catch { label("کپی نشد"); }
    setTimeout(() => label("کپی"), 1500);
  });
  wrap.append(btn);
}
