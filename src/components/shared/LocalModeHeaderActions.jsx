import { RefreshCw } from 'lucide-react';

import { Badge, Button } from '../ui';

export function LocalModeHeaderActions({
    databaseFree = false,
    onRefresh,
    refreshing = false,
    disabled = false,
    refreshLabel = 'Refresh',
}) {
    return (
        <>
            {databaseFree && <Badge tone="info">Database-free</Badge>}
            <Button
                variant="secondary"
                size="icon"
                onClick={onRefresh}
                disabled={disabled || refreshing}
                title={refreshLabel}
                aria-label={refreshLabel}
            >
                <RefreshCw size={16} className={refreshing ? 'animate-spin' : ''} aria-hidden="true" />
            </Button>
        </>
    );
}

export default LocalModeHeaderActions;
