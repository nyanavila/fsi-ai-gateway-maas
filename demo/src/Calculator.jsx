import { useState, useEffect, useRef } from "react";

const PRICES = { haiku: 0.80, sonnet: 9.00, opus: 75.00 };

function fmt(n) {
  if (n >= 1000000) return "$" + (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return "$" + (n / 1000).toFixed(1) + "K";
  return "$" + n.toFixed(0);
}

function Slider({ label, id, min, max, step, value, onChange, display }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13,
        color: "var(--color-text-secondary)", marginBottom: 6 }}>
        <span>{label}</span>
        <span style={{ fontWeight: 500, color: "var(--color-text-primary)" }}>{display}</span>
      </div>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(Number(e.target.value))} style={{ width: "100%" }} />
    </div>
  );
}

function MetricCard({ label, value, sub, dark, green }) {
  const bg = dark ? "#0a3d2e" : green ? "#e8f5e9" : "var(--color-background-secondary)";
  const labelColor = dark ? "#5dba8a" : green ? "#1b5e20" : "var(--color-text-secondary)";
  const valueColor = dark ? "#a8f0c6" : green ? "#1b5e20" : "var(--color-text-primary)";
  const subColor   = dark ? "#5dba8a" : green ? "#2e7d32" : "var(--color-text-secondary)";
  return (
    <div style={{ background: bg, borderRadius: "var(--border-radius-md)", padding: "1rem" }}>
      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: labelColor,
        letterSpacing: "0.1em", marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 500, color: valueColor }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: subColor, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

export default function Calculator() {
  const [queries,   setQueries]   = useState(50000);
  const [tokens,    setTokens]    = useState(500);
  const [simple,    setSimple]    = useState(60);
  const [complex,   setComplex]   = useState(30);
  const [escal,     setEscal]     = useState(10);
  const [cacheRate, setCacheRate] = useState(35);
  const chartRef = useRef(null);
  const chartInstance = useRef(null);

  const tokensM     = tokens / 1_000_000;
  const liveQ       = queries * (1 - cacheRate / 100);
  const costWithout = queries * tokensM * PRICES.opus;
  const costRouting = liveQ * tokensM * (
    (simple / 100) * PRICES.haiku +
    (complex / 100) * PRICES.sonnet +
    (escal / 100) * PRICES.opus
  );
  const cacheCost   = queries * (cacheRate / 100) * 0.002;
  const costWith    = costRouting + cacheCost;
  const saving      = costWithout - costWith;
  const routingSave = costWithout - costRouting;
  const cacheSave   = Math.max(0, costRouting - costWith);
  const pctSaved    = costWithout > 0 ? Math.round((saving / costWithout) * 100) : 0;

  function adjustMix(changed, val) {
    const vals = { simple, complex, escal, [changed]: val };
    const others = Object.keys(vals).filter(k => k !== changed);
    const remainder = 100 - val;
    const otherSum = others.reduce((s, k) => s + vals[k], 0);
    if (otherSum === 0) {
      const each = Math.round(remainder / others.length);
      others.forEach(k => vals[k] = each);
    } else {
      others.forEach(k => { vals[k] = Math.round((vals[k] / otherSum) * remainder); });
    }
    const total = Object.values(vals).reduce((s, v) => s + v, 0);
    if (total !== 100) vals[others[0]] += 100 - total;
    setSimple(vals.simple);
    setComplex(vals.complex);
    setEscal(vals.escal);
  }

  useEffect(() => {
    if (!chartRef.current) return;
    const withoutData = [
      queries * (simple/100) * tokensM * PRICES.opus,
      queries * (complex/100) * tokensM * PRICES.opus,
      queries * (escal/100) * tokensM * PRICES.opus,
    ];
    const withData = [
      liveQ * (simple/100) * tokensM * PRICES.haiku,
      liveQ * (complex/100) * tokensM * PRICES.sonnet,
      liveQ * (escal/100) * tokensM * PRICES.opus + cacheCost,
    ];
    if (chartInstance.current) {
      chartInstance.current.data.datasets[0].data = withoutData;
      chartInstance.current.data.datasets[1].data = withData;
      chartInstance.current.update("none");
    } else if (window.Chart) {
      chartInstance.current = new window.Chart(chartRef.current, {
        type: "bar",
        data: {
          labels: ["Haiku  (simple)", "Sonnet  (complex)", "Opus  (escalations)"],
          datasets: [
            { label: "Without gateway", data: withoutData,
              backgroundColor: "#c62828", borderRadius: 4 },
            { label: "With gateway",    data: withData,
              backgroundColor: "#2e7d32", borderRadius: 4 },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { font: { size: 11 } } },
            y: { ticks: { callback: v => fmt(v), font: { size: 11 } } },
          },
        },
      });
    }
  }, [queries, tokens, simple, complex, escal, cacheRate]);

  return (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: "32px 24px 60px" }}>

      <div style={{ fontSize: 11, fontFamily: "var(--font-mono)",
        color: "#5dba8a", letterSpacing: "0.15em", marginBottom: 8 }}>
        COST SAVINGS CALCULATOR
      </div>
      <div style={{ fontSize: 13, color: "#a8c8b8", marginBottom: 28 }}>
        Adjust your traffic profile to model expected savings from intelligent routing and semantic caching.
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>

        <div style={{ background: "var(--color-background-primary)",
          border: "0.5px solid var(--color-border-tertiary)",
          borderRadius: "var(--border-radius-lg)", padding: "1.25rem" }}>

          <div style={{ fontSize: 11, fontFamily: "var(--font-mono)",
            color: "var(--color-text-secondary)", letterSpacing: "0.1em", marginBottom: 16 }}>
            TRAFFIC PROFILE
          </div>

          <Slider label="Daily queries" min={1000} max={500000} step={1000}
            value={queries} onChange={setQueries}
            display={queries.toLocaleString()} />

          <Slider label="Avg tokens per query" min={100} max={2000} step={100}
            value={tokens} onChange={setTokens}
            display={tokens.toLocaleString()} />

          <Slider label="Cache hit rate" min={0} max={80} step={5}
            value={cacheRate} onChange={setCacheRate}
            display={cacheRate + "%"} />

          <div style={{ fontSize: 11, fontFamily: "var(--font-mono)",
            color: "var(--color-text-secondary)", letterSpacing: "0.1em",
            margin: "18px 0 12px" }}>
            ROUTING MIX
          </div>

          <Slider label="Simple queries → Haiku" min={0} max={100} step={5}
            value={simple} onChange={v => adjustMix("simple", v)}
            display={simple + "%"} />

          <Slider label="Complex queries → Sonnet" min={0} max={100} step={5}
            value={complex} onChange={v => adjustMix("complex", v)}
            display={complex + "%"} />

          <Slider label="Escalations → Opus" min={0} max={100} step={5}
            value={escal} onChange={v => adjustMix("escal", v)}
            display={escal + "%"} />

          <div style={{ fontSize: 11, color: "var(--color-text-secondary)",
            fontFamily: "var(--font-mono)", textAlign: "right" }}>
            mix total: {simple + complex + escal}%
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <MetricCard label="WITHOUT GATEWAY"
            value={fmt(costWithout)}
            sub="daily · everything hits Opus" />

          <MetricCard label="WITH GATEWAY"
            value={fmt(costWith)}
            sub="daily · routing + cache"
            green />

          <MetricCard label={`DAILY SAVING  ·  ${pctSaved}% reduction`}
            value={fmt(saving)}
            sub={fmt(saving * 365) + " per year"}
            dark />

          <div style={{ background: "var(--color-background-primary)",
            border: "0.5px solid var(--color-border-tertiary)",
            borderRadius: "var(--border-radius-md)", padding: "1rem" }}>
            <div style={{ fontSize: 11, fontFamily: "var(--font-mono)",
              color: "var(--color-text-secondary)", letterSpacing: "0.1em", marginBottom: 10 }}>
              SAVING BREAKDOWN
            </div>
            {[
              ["Smart routing",   fmt(routingSave)],
              ["Semantic cache",  fmt(cacheSave)],
            ].map(([label, val]) => (
              <div key={label} style={{ display: "flex", justifyContent: "space-between",
                fontSize: 13, color: "var(--color-text-secondary)",
                padding: "4px 0", borderBottom: "0.5px solid var(--color-border-tertiary)" }}>
                <span>{label}</span>
                <span style={{ fontWeight: 500, color: "var(--color-text-primary)" }}>{val}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div style={{ background: "var(--color-background-primary)",
        border: "0.5px solid var(--color-border-tertiary)",
        borderRadius: "var(--border-radius-lg)", padding: "1.25rem", marginBottom: 16 }}>
        <div style={{ fontSize: 11, fontFamily: "var(--font-mono)",
          color: "var(--color-text-secondary)", letterSpacing: "0.1em", marginBottom: 14 }}>
          MODEL PRICING  ·  per 1M tokens (input + output blended)
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
          {[
            { name: "Haiku", price: "$0.80", color: "#4caf50",
              use: "Simple queries", eg: "FAQs, account info, password resets, status checks" },
            { name: "Sonnet", price: "$9.00", color: "#2196f3",
              use: "Complex queries", eg: "Disputes, refunds, transactions, policy questions" },
            { name: "Opus", price: "$75.00", color: "#e91e63",
              use: "Escalations", eg: "Angry customers, fraud claims, high-value complaints" },
          ].map(m => (
            <div key={m.name}>
              <div style={{ display: "flex", alignItems: "center", gap: 8,
                paddingBottom: 8, borderBottom: "0.5px solid var(--color-border-tertiary)",
                marginBottom: 8 }}>
                <div style={{ width: 10, height: 10, borderRadius: 2,
                  background: m.color, flexShrink: 0 }} />
                <span style={{ fontSize: 13, fontWeight: 500,
                  color: "var(--color-text-primary)" }}>{m.name}</span>
                <span style={{ marginLeft: "auto", fontSize: 13, fontWeight: 500,
                  color: "var(--color-text-primary)" }}>{m.price}</span>
              </div>
              <div style={{ fontSize: 11, fontFamily: "var(--font-mono)",
                color: "var(--color-text-secondary)", marginBottom: 4 }}>{m.use}</div>
              <div style={{ fontSize: 12, color: "var(--color-text-secondary)",
                lineHeight: 1.5 }}>{m.eg}</div>
            </div>
          ))}
        </div>
      </div>

      <div style={{ background: "var(--color-background-primary)",
        border: "0.5px solid var(--color-border-tertiary)",
        borderRadius: "var(--border-radius-lg)", padding: "1.25rem" }}>
        <div style={{ fontSize: 11, fontFamily: "var(--font-mono)",
          color: "var(--color-text-secondary)", letterSpacing: "0.1em", marginBottom: 4 }}>
          COST PER TIER  ·  without vs with gateway
        </div>
        <div style={{ display: "flex", gap: 16, marginBottom: 12, fontSize: 12,
          color: "var(--color-text-secondary)" }}>
          <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2,
              background: "#c62828", display: "inline-block" }} />
            Without gateway
          </span>
          <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2,
              background: "#2e7d32", display: "inline-block" }} />
            With gateway
          </span>
        </div>
        <div style={{ position: "relative", width: "100%", height: 200 }}>
          <canvas ref={chartRef} role="img"
            aria-label="Bar chart comparing daily costs per model tier with and without the AI gateway">
            Cost comparison by model tier
          </canvas>
        </div>
      </div>

      <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js" />
    </div>
  );
}
