/* Interactive lessons ("explorables"): a step player + reusable SVG widgets.
 *
 * A lesson is a list of steps. Each step can have text, a widget, a goal (a condition on the
 * widget's state that unlocks Continue) and a question. Questions marked `predict` are asked
 * BEFORE the widget unlocks -- predict first, then see (the pattern that makes these work).
 * Answers are checked by the server's symbolic grader (/api/lesson/check); lesson steps are
 * guided practice, so they never count toward mastery.
 */
"use strict";
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const svg = (tag, attrs = {}, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  };
  const css = getComputedStyle(document.documentElement);
  const C = (name) => css.getPropertyValue(name).trim() || "#888";
  const fmt = (v, d = 3) => (Math.abs(v) < 1e-9 ? 0 : +v.toFixed(d)).toString();
  const complexStr = (a, b) => {
    a = +a.toFixed(2); b = +b.toFixed(2);
    if (!b) return `${a}`;
    const bi = Math.abs(b) === 1 ? "i" : `${Math.abs(b)}i`;
    return a ? `${a} ${b < 0 ? "−" : "+"} ${bi}` : `${b < 0 ? "−" : ""}${bi}`;
  };
  const pointer = (svgEl, evt) => {
    const pt = svgEl.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY;
    return pt.matrixTransform(svgEl.getScreenCTM().inverse());
  };

  // ------------------------------------------------------------- widget: algebra tiles
  // Completing the square: x² square + b·x strip; split the strip, fill the missing corner.
  function tiles(el, cfg, emit) {
    const st = { b: cfg.b ?? 6, split: !!cfg.split, filled: !!cfg.filled };
    const X = 150, U = 15; // x drawn as 150px, one unit as 15px
    el.innerHTML = `<div class="widget-box"><svg viewBox="-10 -10 330 330" class="wsvg" role="img" aria-label="algebra tiles"></svg>
      <div class="row wctl">${cfg.slider ? `<label class="faint" style="font-size:.85rem">b = <b id="bv">${st.b}</b> <input id="bs" type="range" min="2" max="10" step="2" value="${st.b}" style="width:140px"></label>` : ""}
      <button class="btn sm" id="split">✂️ Split the strip</button><button class="btn sm" id="fill">➕ Fill the corner</button><button class="btn sm ghost" id="reset">Reset</button></div>
      <div class="weq" id="eq"></div></div>`;
    const s = el.querySelector("svg");
    const draw = () => {
      s.innerHTML = "";
      const w = st.b * U, h = w / 2;
      svg("rect", { x: 0, y: 0, width: X, height: X, fill: C("--accent"), "fill-opacity": .35, stroke: C("--accent"), "stroke-width": 2, rx: 3 }, s);
      svg("text", { x: X / 2, y: X / 2 + 8, "text-anchor": "middle", "font-size": 26, fill: C("--ink") }, s).textContent = "x²";
      const strip = (x, y, ww, hh, label) => {
        svg("rect", { x, y, width: ww, height: hh, fill: C("--heat-2"), "fill-opacity": .55, stroke: C("--heat-3"), "stroke-width": 2, rx: 3, class: "tile-anim" }, s);
        for (let k = 1; k < Math.round((ww > hh ? ww : hh) / U); k++) {
          const t = k * U;
          svg("line", ww > hh ? { x1: x + t, y1: y, x2: x + t, y2: y + hh } : { x1: x, y1: y + t, x2: x + ww, y2: y + t }, s).setAttribute("stroke", C("--heat-3"));
        }
        const tx = svg("text", { x: x + ww / 2, y: y + hh / 2 + 6, "text-anchor": "middle", "font-size": 15, fill: C("--ink") }, s);
        tx.textContent = label;
        if (ww < hh) tx.setAttribute("transform", `rotate(-90 ${x + ww / 2} ${y + hh / 2})`);
      };
      if (!st.split) strip(X + 4, 0, w, X, `${st.b}x`);
      else {
        strip(X + 4, 0, h, X, `${st.b / 2}x`);
        strip(0, X + 4, X, h, `${st.b / 2}x`);
        const cx = X + 4, cy = X + 4;
        if (st.filled) {
          svg("rect", { x: cx, y: cy, width: h, height: h, fill: C("--good"), "fill-opacity": .5, stroke: C("--good"), "stroke-width": 2, rx: 2, class: "tile-anim" }, s);
          for (let i = 1; i < st.b / 2; i++) {
            svg("line", { x1: cx + i * U, y1: cy, x2: cx + i * U, y2: cy + h, stroke: C("--good") }, s);
            svg("line", { x1: cx, y1: cy + i * U, x2: cx + h, y2: cy + i * U, stroke: C("--good") }, s);
          }
          svg("rect", { x: -3, y: -3, width: X + 4 + h + 6, height: X + 4 + h + 6, fill: "none", stroke: C("--ink-2"), "stroke-dasharray": "6 5", rx: 4 }, s);
        } else {
          svg("rect", { x: cx, y: cy, width: h, height: h, fill: "none", stroke: C("--bad"), "stroke-dasharray": "5 4", "stroke-width": 2 }, s);
          svg("text", { x: cx + h / 2, y: cy + h / 2 + 5, "text-anchor": "middle", "font-size": 13, fill: C("--bad") }, s).textContent = "?";
        }
        svg("text", { x: X + 4 + h / 2, y: -1, "text-anchor": "middle", "font-size": 12, fill: C("--ink-3") }, s).textContent = `${st.b / 2}`;
      }
      svg("text", { x: X / 2, y: -1, "text-anchor": "middle", "font-size": 12, fill: C("--ink-3") }, s).textContent = "x";
      const half = st.b / 2;
      el.querySelector("#eq").innerHTML = !st.split ? `$x^2+${st.b}x$` : !st.filled
        ? `$x^2+${st.b}x$ is almost a square… it's missing a $${half}\\times${half}$ corner`
        : `$x^2+${st.b}x+${half * half}=(x+${half})^2$`;
      if (window.renderMathInElement) renderMathInElement(el.querySelector("#eq"), { delimiters: [{ left: "$", right: "$", display: false }], throwOnError: false });
      emit({ ...st });
    };
    el.querySelector("#split").onclick = () => { st.split = true; draw(); };
    el.querySelector("#fill").onclick = () => { if (st.split) { st.filled = true; draw(); } };
    el.querySelector("#reset").onclick = () => { st.split = false; st.filled = false; draw(); };
    const bs = el.querySelector("#bs");
    if (bs) bs.oninput = () => { st.b = +bs.value; el.querySelector("#bv").textContent = st.b; st.split = false; st.filled = false; draw(); };
    draw();
    return { lock(on) { el.querySelectorAll("button,input").forEach(x => x.disabled = on); } };
  }

  // ------------------------------------------------------------- widget: complex plane
  // Drag z; multiply by i, -1 or w; watch the rotation (angles add, lengths multiply).
  function complexPlane(el, cfg, emit) {
    const R = cfg.range ?? 5, S = 300 / (2 * R);
    const st = { a: cfg.z?.[0] ?? 3, b: cfg.z?.[1] ?? 0, times_i: 0, times_neg1: 0, times_w: 0 };
    const w = cfg.w; // e.g. [1, 1]
    const ops = cfg.ops || ["i"];
    el.innerHTML = `<div class="widget-box"><svg viewBox="0 0 300 300" class="wsvg" style="touch-action:none" role="img" aria-label="complex plane"></svg>
      <div class="row wctl">${ops.map(o => `<button class="btn sm" data-op="${o}">× ${o === "i" ? "i" : o === "-1" ? "(−1)" : `(${complexStr(w[0], w[1])})`}</button>`).join("")}<button class="btn sm ghost" id="rz">Reset</button></div>
      <div class="weq" id="read"></div></div>`;
    const s = el.querySelector("svg");
    const X = a => 150 + a * S, Y = b => 150 - b * S;
    const trail = [];
    let anim = null;
    const draw = (ghost) => {
      s.innerHTML = "";
      for (let k = -R; k <= R; k++) {
        svg("line", { x1: X(k), y1: 0, x2: X(k), y2: 300, stroke: C("--line"), "stroke-width": k ? 1 : 2 }, s);
        svg("line", { x1: 0, y1: Y(k), x2: 300, y2: Y(k), stroke: C("--line"), "stroke-width": k ? 1 : 2 }, s);
      }
      svg("text", { x: 292, y: 144, "text-anchor": "end", "font-size": 11, fill: C("--ink-3") }, s).textContent = "real";
      svg("text", { x: 156, y: 12, "font-size": 11, fill: C("--ink-3") }, s).textContent = "imaginary";
      svg("circle", { cx: 150, cy: 150, r: Math.hypot(st.a, st.b) * S, fill: "none", stroke: C("--line-2"), "stroke-dasharray": "3 4" }, s);
      trail.forEach((p, k) => svg("circle", { cx: X(p[0]), cy: Y(p[1]), r: 4, fill: C("--ink-3"), opacity: .25 + .5 * k / Math.max(1, trail.length) }, s));
      const [pa, pb] = ghost || [st.a, st.b];
      svg("line", { x1: 150, y1: 150, x2: X(pa), y2: Y(pb), stroke: C("--accent"), "stroke-width": 3, "stroke-linecap": "round" }, s);
      const dot = svg("circle", { cx: X(pa), cy: Y(pb), r: 10, fill: C("--accent"), stroke: C("--surface"), "stroke-width": 3, style: "cursor:grab" }, s);
      dot.addEventListener("pointerdown", startDrag);
      const r = Math.hypot(st.a, st.b), ang = (Math.atan2(st.b, st.a) * 180 / Math.PI + 360) % 360;
      el.querySelector("#read").innerHTML = `$z = ${complexStr(st.a, st.b).replace("−", "-")}$ &nbsp;·&nbsp; length $|z| = ${fmt(r, 2)}$ &nbsp;·&nbsp; angle $${fmt(ang, 0)}^\\circ$`;
      if (window.renderMathInElement) renderMathInElement(el.querySelector("#read"), { delimiters: [{ left: "$", right: "$", display: false }], throwOnError: false });
    };
    let dragging = false;
    function startDrag(e) { if (el.dataset.locked === "1" || cfg.fixed) return; dragging = true; s.setPointerCapture(e.pointerId); }
    s.addEventListener("pointermove", e => {
      if (!dragging) return;
      const p = pointer(s, e);
      st.a = Math.max(-R, Math.min(R, Math.round(((p.x - 150) / S) * 2) / 2));
      st.b = Math.max(-R, Math.min(R, Math.round(((150 - p.y) / S) * 2) / 2));
      draw(); emit({ ...st });
    });
    s.addEventListener("pointerup", () => { dragging = false; });
    const multiply = (m) => {
      if (anim) return;
      const [ma, mb] = m;
      const na = st.a * ma - st.b * mb, nb = st.a * mb + st.b * ma;
      const r0 = Math.hypot(st.a, st.b), r1 = Math.hypot(na, nb);
      const t0 = Math.atan2(st.b, st.a);
      let dt = Math.atan2(mb, ma);
      trail.push([st.a, st.b]); if (trail.length > 8) trail.shift();
      const start = performance.now(), dur = 650;
      const step = (now) => {
        const u = Math.min(1, (now - start) / dur), e = u < .5 ? 2 * u * u : 1 - (-2 * u + 2) ** 2 / 2;
        const rr = r0 + (r1 - r0) * e, tt = t0 + dt * e;
        draw([rr * Math.cos(tt), rr * Math.sin(tt)]);
        if (u < 1) anim = requestAnimationFrame(step);
        else { anim = null; st.a = +na.toFixed(6); st.b = +nb.toFixed(6); draw(); emit({ ...st }); }
      };
      anim = requestAnimationFrame(step);
    };
    el.querySelectorAll("[data-op]").forEach(b => b.onclick = () => {
      const o = b.dataset.op;
      if (o === "i") { st.times_i++; multiply([0, 1]); }
      else if (o === "-1") { st.times_neg1++; multiply([-1, 0]); }
      else { st.times_w++; multiply(w); }
    });
    el.querySelector("#rz").onclick = () => { st.a = cfg.z?.[0] ?? 3; st.b = cfg.z?.[1] ?? 0; trail.length = 0; draw(); emit({ ...st }); };
    draw(); emit({ ...st });
    return { lock(on) { el.dataset.locked = on ? "1" : "0"; el.querySelectorAll("button").forEach(x => x.disabled = on); } };
  }

  // ------------------------------------------------------------- widget: zoom & secant
  // Zoom into a curve at x=a until it looks straight; shrink h to turn a secant into a tangent.
  const FUNCS = {
    "x^2": [x => x * x, "x^2"], "sin": [Math.sin, "\\sin x"], "exp": [Math.exp, "e^x"],
    "abs": [Math.abs, "|x|"], "x^3": [x => x ** 3, "x^3"],
  };
  function zoomer(el, cfg, emit) {
    const [f, tex] = FUNCS[cfg.f || "x^2"];
    const a = cfg.a ?? 1;
    const st = { zoom: 1, h: cfg.secant ? 1 : null, slope: null };
    el.innerHTML = `<div class="widget-box"><svg viewBox="0 0 300 220" class="wsvg" role="img" aria-label="function zoom"></svg>
      <div class="stack wctl" style="gap:6px">
        <label class="faint" style="font-size:.85rem">🔍 zoom ×<b id="zv">1</b><input id="zs" type="range" min="0" max="3" step="0.01" value="0" style="width:100%"></label>
        ${cfg.secant ? `<label class="faint" style="font-size:.85rem">h = <b id="hv">1</b> <input id="hs" type="range" min="-3" max="0" step="0.01" value="0" style="width:100%"></label>` : ""}
      </div><div class="weq" id="read"></div></div>`;
    const s = el.querySelector("svg");
    const draw = () => {
      s.innerHTML = "";
      const half = 2.5 / st.zoom, fa = f(a);
      const x0 = a - half, x1 = a + half;
      // y-window keeps the point centred and the curve's scale honest (same zoom on both axes)
      const yh = half * 220 / 300, y0 = fa - yh, y1 = fa + yh;
      const X = x => (x - x0) / (x1 - x0) * 300, Y = y => 220 - (y - y0) / (y1 - y0) * 220;
      svg("line", { x1: 0, y1: Y(fa), x2: 300, y2: Y(fa), stroke: C("--line") }, s);
      svg("line", { x1: X(a), y1: 0, x2: X(a), y2: 220, stroke: C("--line") }, s);
      let d = "";
      for (let k = 0; k <= 300; k++) { const x = x0 + (x1 - x0) * k / 300, y = f(x); d += `${k ? "L" : "M"}${X(x).toFixed(2)},${Y(y).toFixed(2)}`; }
      svg("path", { d, fill: "none", stroke: C("--heat-3"), "stroke-width": 3, "stroke-linejoin": "round" }, s);
      if (st.h !== null) {
        const b = a + st.h, m = (f(b) - fa) / st.h;
        st.slope = m;
        const xa = x0 - 10 * half, xb = x1 + 10 * half;
        svg("line", { x1: X(xa), y1: Y(fa + m * (xa - a)), x2: X(xb), y2: Y(fa + m * (xb - a)), stroke: C("--accent"), "stroke-width": 2, "stroke-dasharray": "6 4" }, s);
        svg("circle", { cx: X(b), cy: Y(f(b)), r: 6, fill: C("--accent") }, s);
      }
      svg("circle", { cx: X(a), cy: Y(fa), r: 6, fill: C("--ink"), stroke: C("--surface"), "stroke-width": 2 }, s);
      svg("text", { x: 8, y: 16, "font-size": 12, fill: C("--ink-3") }, s).textContent = `window: x from ${fmt(x0, 4)} to ${fmt(x1, 4)}`;
      el.querySelector("#read").innerHTML = `$f(x)=${tex}$ at $x=${a}$` + (st.h !== null ? ` &nbsp;·&nbsp; secant slope $\\dfrac{f(${a}+h)-f(${a})}{h} = ${fmt(st.slope, 4)}$` : "");
      if (window.renderMathInElement) renderMathInElement(el.querySelector("#read"), { delimiters: [{ left: "$", right: "$", display: false }], throwOnError: false });
      emit({ ...st });
    };
    const zs = el.querySelector("#zs"), hs = el.querySelector("#hs");
    zs.oninput = () => { st.zoom = +(10 ** +zs.value).toFixed(2); el.querySelector("#zv").textContent = st.zoom; draw(); };
    if (hs) hs.oninput = () => { st.h = +(10 ** +hs.value).toPrecision(2); el.querySelector("#hv").textContent = st.h; draw(); };
    draw();
    return { lock(on) { el.querySelectorAll("input").forEach(x => x.disabled = on); } };
  }

  const WIDGETS = { tiles, complex: complexPlane, zoom: zoomer };

  // ------------------------------------------------------------- goals
  function goalMet(goal, state) {
    if (!goal) return true;
    return Object.entries(goal).every(([k, v]) => {
      const [key, op] = k.split("__");
      const x = state[key];
      if (op === "gte") return x >= v;
      if (op === "lte") return x !== null && x <= v;
      return x === v;
    });
  }

  // ------------------------------------------------------------- the player
  window.runLesson = async function runLesson(host, lesson, api, hooks) {
    const steps = lesson.steps;
    let i = 0, correct = 0, asked = 0;
    const rich = hooks.rich, math = hooks.math, esc = hooks.esc;
    const draw = () => {
      if (i >= steps.length) return hooks.finish({ correct, total: asked });
      const step = steps[i];
      host.innerHTML = `<div class="row" style="margin-bottom:10px"><div class="progress-dots">${steps.map((_, k) => `<i class="${k < i ? "ok" : k === i ? "cur" : ""}"></i>`).join("")}</div><span class="spacer"></span><span class="faint" style="font-size:.8rem">${i + 1} / ${steps.length}</span></div>
        <div class="card stack">
          ${step.text ? `<div class="q-prompt">${rich(step.text)}</div>` : ""}
          <div id="w"></div>
          <div id="ask"></div>
          <div id="goal" class="faint" style="font-size:.85rem">${step.goal_hint ? "🎯 " + rich(step.goal_hint) : ""}</div>
          <div class="row"><button class="btn primary" id="next" disabled>${i === steps.length - 1 ? "Finish" : "Continue →"}</button></div>
        </div>`;
      math(host);
      let state = {}, answered = !step.ask, widget = null;
      const next = host.querySelector("#next");
      const refresh = () => {
        const ok = goalMet(step.goal, state) && answered;
        next.disabled = !ok;
        if (step.goal && goalMet(step.goal, state) && step.goal_hint) host.querySelector("#goal").innerHTML = "✅ " + rich(step.goal_done || "Nice.");
        math(host.querySelector("#goal"));
      };
      if (step.widget) {
        const W = WIDGETS[step.widget.type];
        widget = W(host.querySelector("#w"), step.widget, (s) => { state = s; refresh(); });
      }
      if (step.ask) {
        const q = step.ask;
        const box = host.querySelector("#ask");
        if (q.predict && widget) widget.lock(true);
        box.innerHTML = `<div class="verdict warn" style="margin-top:0"><div class="eyebrow">${q.predict ? "🤔 Predict first" : "✏️ Your turn"}</div>
          <div style="margin-top:6px">${rich(q.prompt)}</div>
          ${q.type === "mc" ? `<div class="choices">${q.choices.map((c, k) => `<button class="choice" data-k="${k}">${rich(c)}</button>`).join("")}</div>`
            : `<div class="row" style="margin-top:8px"><input id="la" placeholder="${esc(q.placeholder || "your answer")}" autocomplete="off" autocapitalize="off" style="flex:1;min-width:160px"><button class="btn sm primary" id="lc">Check</button></div>`}
          <div id="lfb"></div></div>`;
        math(box);
        const done = (ok, fb) => {
          asked++; if (ok) correct++;
          answered = true;
          if (widget) widget.lock(false);
          box.querySelectorAll("button,input").forEach(x => x.disabled = true);
          box.querySelector("#lfb").innerHTML = `<div style="margin-top:8px"><b>${ok ? "✅ Yes." : q.predict ? "Interesting guess, now look." : "Not quite."}</b> ${q.explain ? rich(q.explain) : ""}${fb ? `<div class="faint">${esc(fb)}</div>` : ""}</div>`;
          math(box); refresh();
        };
        const check = async (given) => {
          const r = await api(`/api/lesson/check`, { type: q.type, answer: String(q.answer), given: String(given), var: q.var || null, choices: q.choices || null });
          if (r.retry) { box.querySelector("#lfb").innerHTML = `<div class="faint" style="margin-top:6px">🤔 ${esc(r.feedback)}. Try again.</div>`; return; }
          done(!!r.correct, "");
        };
        box.querySelectorAll(".choice").forEach(b => b.onclick = () => { b.classList.add(String(+b.dataset.k) === String(q.answer) ? "right" : "wrong"); check(b.dataset.k); });
        const lc = box.querySelector("#lc"), la = box.querySelector("#la");
        if (lc) { lc.onclick = () => la.value.trim() && check(la.value); la.onkeydown = e => { if (e.key === "Enter") lc.click(); }; }
      }
      refresh();
      next.onclick = () => { i++; draw(); };
    };
    draw();
  };
})();
