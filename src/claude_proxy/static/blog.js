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
