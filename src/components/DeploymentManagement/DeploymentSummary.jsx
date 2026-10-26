import { cn } from '../../utils/cn';

function SummaryCard({ label, value, tone = 'text-white' }) {
    return (
        <div className="flex h-full flex-col gap-2 rounded-xl border border-slate-700/60 bg-slate-800/60 p-4 text-left shadow-lg backdrop-blur-xl">
            <div className="truncate text-xs font-medium text-slate-400">{label}</div>
            <div className={cn('truncate text-3xl font-bold leading-none', tone)}>{value}</div>
        </div>
    );
}

export function DeploymentSummary({ summary }) {
    return (
        <section className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                <SummaryCard label="Total" value={summary.total} />
                <SummaryCard label="Ready" value={summary.Ready} />
                <SummaryCard label="In progress" value={summary['In progress']} />
                <SummaryCard
                    label="Failed"
                    value={summary.Failed}
                    tone={summary.Failed > 0 ? 'text-rose-500 font-extrabold' : 'text-white'}
                />
                <SummaryCard label="Cleaned" value={summary.Cleaned} />
            </div>
        </section>
    );
}

export default DeploymentSummary;
