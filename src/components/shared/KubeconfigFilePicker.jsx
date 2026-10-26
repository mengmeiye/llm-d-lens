import { Upload } from 'lucide-react';

import { cn } from '../../utils/cn';

export function KubeconfigFilePicker({
    file,
    onFileChange,
    inputId = 'kubeconfig-file',
    disabled = false,
}) {
    const chooseFile = (event) => {
        onFileChange(event.target.files?.[0] || null);
        event.target.value = '';
    };

    return (
        <div className="flex items-center gap-2 rounded-lg border border-theme-border bg-theme-card px-3 py-2">
            <Upload size={15} className="shrink-0 text-theme-muted" aria-hidden="true" />
            <span className="flex-1 truncate text-sm text-theme-muted">
                {file ? file.name : 'Choose a kubeconfig file'}
            </span>
            <input
                id={inputId}
                type="file"
                onChange={chooseFile}
                className="hidden"
                disabled={disabled}
            />
            <label
                htmlFor={inputId}
                aria-disabled={disabled}
                className={cn(
                    'cursor-pointer rounded-md border border-emerald-500/40 px-2 py-1 text-xs font-medium',
                    'text-emerald-600 hover:bg-emerald-500/10 dark:text-emerald-400',
                    disabled && 'pointer-events-none opacity-50'
                )}
            >
                Browse
            </label>
        </div>
    );
}
