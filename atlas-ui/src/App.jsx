import { useMemo, useState, useEffect } from "react";

const isVerified = (s) => s.score >= 95 && s.status !== "EXPIRED" && s.status !== "REVIEW_REQUIRED";
function pillMeta(s) {
  if (s.status === "EXPIRED") return { bg: "bg-rose", text: `EXPIRED: ${Math.round(s.score)}%` };
  if (isVerified(s)) return { bg: "bg-mint", text: `VERIFIED: ${Math.round(s.score)}%` };
  return { bg: "bg-lemon", text: `REVIEW: ${Math.round(s.score)}%` };
}
const prettyField = (f) => f.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

function Pill({ s }) {
  const { bg, text } = pillMeta(s);
  return <span className={`${bg} inline-block whitespace-nowrap border-2 border-ink rounded-full px-3 py-1 text-xs font-extrabold uppercase text-ink shadow-hard-sm`}>{text}</span>;
}

function Metric({ label, value, dot }) {
  return (
    <div className="border-2 border-ink bg-white p-4 shadow-hard">
      <div className="flex items-center gap-2 text-sm font-bold uppercase tracking-wide">
        {dot && <span className={`inline-block h-4 w-4 rounded-full border-2 border-ink ${dot}`} />}
        {label}
      </div>
      <div className="mt-2 font-display text-5xl font-extrabold tracking-tighter">{value}</div>
    </div>
  );
}

function Accordion({ ev, open, onToggle }) {
  return (
    <div className="border-2 border-ink bg-white shadow-hard mb-4">
      <button onClick={onToggle} className="flex w-full items-center justify-between px-4 py-3 text-left font-display text-lg font-extrabold tracking-tighter">
        <span>{prettyField(ev.field)} <span className="font-sans text-sm font-medium">→ {ev.value}</span></span>
        <span className="flex h-7 w-7 items-center justify-center border-2 border-ink bg-ink text-white">{open ? "−" : "+"}</span>
      </button>
      {open && (
        <div className="border-t-2 border-ink bg-zinc-100 p-4">
          <div className="border-2 border-l-8 border-ink bg-white p-4 font-mono text-sm leading-relaxed">"{ev.snippet}"</div>
        </div>
      )}
    </div>
  );
}

function Inspector({ s }) {
  const [openField, setOpenField] = useState(s.evidence?.[0]?.field);
  const schema = [["Amount", s.amount], ["Deadline", s.deadline], ["Income Criteria", s.income], ["Education Level", s.education]];

  return (
    <div className="space-y-8">
      <header>
        <div className="mb-3"><Pill s={s} /></div>
        <h2 className="text-4xl leading-none">{s.name}</h2>
        <p className="mt-2 font-bold">{s.provider} · {s.source_type}</p>
        <a href={s.url} target="_blank" className="press mt-4 inline-block border-2 border-ink bg-white rounded px-3 py-2 text-sm font-bold shadow-hard">Official Source ↗</a>
      </header>

      <section>
        <h3 className="mb-3 text-2xl">Eligibility Schema</h3>
        <div className="grid grid-cols-2 gap-4">
          {schema.map(([k, v]) => (
            <div key={k} className="border-2 border-ink bg-white p-4">
              <div className="text-xs font-bold uppercase tracking-wide">{k}</div>
              <div className="mt-1 text-lg font-bold">{v}</div>
            </div>
          ))}
        </div>
      </section>

      <section className="border-2 border-ink bg-white p-6 shadow-hard-lg">
        <div className="mb-4 flex items-end justify-between">
          <h3 className="text-2xl">Confidence Score</h3>
          <div className="font-display text-4xl font-extrabold tracking-tighter">{s.score}%</div>
        </div>
        <ul className="space-y-4">
          {s.breakdown.map((b) => (
            <li key={b.label}>
              <div className="flex justify-between text-sm font-bold"><span>{b.label}</span><span>{b.points}/{b.max} pts</span></div>
              <div className="mt-1 h-3 border-2 border-ink bg-white">
                <div className="h-full bg-ink" style={{ width: `${(b.points / b.max) * 100}%` }} />
              </div>
              <div className="mt-1 text-xs">{b.detail}</div>
            </li>
          ))}
        </ul>
      </section>

      <section>
        <h3 className="mb-1 text-2xl">Traceable Evidence</h3>
        <div className="mt-4">
          {s.evidence?.map((ev) => (
            <Accordion key={ev.field} ev={ev} open={openField === ev.field} onToggle={() => setOpenField(openField === ev.field ? null : ev.field)} />
          ))}
        </div>
      </section>
    </div>
  );
}

function Repository({ scholarships }) {
  const [q, setQ] = useState("");
  const [selectedId, setSelectedId] = useState(scholarships[0]?.id);
  const rows = useMemo(() => scholarships.filter((s) => (s.name + s.provider).toLowerCase().includes(q.toLowerCase())), [q, scholarships]);
  const selected = scholarships.find((s) => s.id === selectedId) ?? rows[0];

  return (
    <div className="grid min-h-[calc(100vh-20rem)] grid-cols-1 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <div className="border-ink bg-white p-6 lg:border-r-2">
        <input className="field mb-6" placeholder="Search scholarships..." value={q} onChange={(e) => setQ(e.target.value)} />
        <div className="space-y-5">
          {rows.map((s) => (
            <button key={s.id} onClick={() => setSelectedId(s.id)} className={`press block w-full border-2 border-ink p-4 text-left shadow-hard ${selectedId === s.id ? "pressed" : "bg-white"}`}>
              <div className="flex justify-between gap-3">
                <div className="min-w-0"><div className="truncate text-lg font-bold">{s.name}</div><div className="text-sm">{s.provider}</div></div>
                <Pill s={s} />
              </div>
            </button>
          ))}
        </div>
      </div>
      <div className="bg-zinc-100 p-8">{selected && <Inspector key={selected.id} s={selected} />}</div>
    </div>
  );
}

export default function App() {
  const [crawling, setCrawling] = useState(false);
  const [scholarships, setScholarships] = useState([]);

  const fetchData = async () => {
    try {
      const res = await fetch("http://localhost:8000/api/scholarships");
      const data = await res.json();
      setScholarships(data);
    } catch (err) { console.error("Is FastAPI running?", err); }
  };

  useEffect(() => { fetchData(); }, []);

  const triggerCrawl = async () => {
    setCrawling(true);
    try {
      await fetch("http://localhost:8000/api/crawl", { method: "POST" });
      setTimeout(async () => { await fetchData(); setCrawling(false); }, 6000);
    } catch (e) { setCrawling(false); }
  };

  const verified = scholarships.filter(isVerified).length;
  const avg = scholarships.length ? (scholarships.reduce((a, s) => a + s.score, 0) / scholarships.length).toFixed(1) : 0;

  return (
    <div className="min-h-screen bg-white">
      <nav className="fixed inset-x-0 top-0 z-50 flex h-20 items-center justify-between border-b-2 border-ink bg-white px-6">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center bg-ink font-display font-extrabold text-white">A</div>
          <span className="font-display text-2xl font-extrabold tracking-tighter">ATLAS Intelligence</span>
        </div>
        <button onClick={triggerCrawl} disabled={crawling} className="press border-2 border-ink bg-ink rounded px-4 py-2 font-bold text-white shadow-hard disabled:opacity-70">
          {crawling ? "Crawling…" : "Trigger Live Crawl"}
        </button>
      </nav>

      <main className="pt-20">
        <section className="grid grid-cols-2 gap-6 border-b-2 border-ink bg-zinc-100 p-6 lg:grid-cols-5">
          <Metric label="Discovered" value={scholarships.length} />
          <Metric label="Verified" value={verified} dot="bg-mint" />
          <Metric label="Review" value={scholarships.length - verified} dot="bg-lemon" />
          <Metric label="Avg Score" value={`${avg}%`} />
        </section>
        <Repository scholarships={scholarships} />
      </main>
    </div>
  );
}