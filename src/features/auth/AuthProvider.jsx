import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AuthContext } from './AuthContext';
import { fetchSession, login as loginRequest, logout as logoutRequest } from './authClient';
import {
    canAccessLocalNamespaceView,
    canAccessView,
    hasPermission,
    LOCAL_NAMESPACE_PERMISSIONS,
} from './permissions';
const LOCAL_NAMESPACE_PRINCIPAL = Object.freeze({
    username: 'Local namespace operator',
    permissions: LOCAL_NAMESPACE_PERMISSIONS,
});

/**
 * Holds the current principal and exposes permission helpers. Hosted sessions
 * mirror the revocable httpOnly cookie resolved by /api/v1/auth/session. The
 * explicit local mode supplies a loopback-only namespace-operator affordance;
 * it is not a server-side authorization boundary.
 */
export function AuthProvider({ children, sessionMode = 'hosted' }) {
    const localSession = sessionMode === 'local';
    const [status, setStatus] = useState(localSession ? 'authenticated' : 'loading');
    const [principal, setPrincipal] = useState(localSession ? LOCAL_NAMESPACE_PRINCIPAL : null);

    const refresh = useCallback(async () => {
        if (localSession) {
            setPrincipal(LOCAL_NAMESPACE_PRINCIPAL);
            setStatus('authenticated');
            return LOCAL_NAMESPACE_PRINCIPAL;
        }
        try {
            const data = await fetchSession();
            setPrincipal(data);
            setStatus('authenticated');
            return data;
        } catch {
            setPrincipal(null);
            setStatus('anonymous');
            return null;
        }
    }, [localSession]);

    useEffect(() => {
        if (localSession) return undefined;
        refresh();
        const onExpired = () => {
            setPrincipal(null);
            setStatus('anonymous');
        };
        window.addEventListener('prism:auth-expired', onExpired);
        return () => window.removeEventListener('prism:auth-expired', onExpired);
    }, [localSession, refresh]);

    const login = useCallback(async (username, password, remember = false) => {
        const data = await loginRequest(username, password, remember);
        setPrincipal(data.principal);
        setStatus('authenticated');
        return data;
    }, []);

    const logout = useCallback(async () => {
        if (localSession) return;
        try {
            await logoutRequest();
        } finally {
            setPrincipal(null);
            setStatus('anonymous');
        }
    }, [localSession]);

    const value = useMemo(() => {
        const permissions = principal?.permissions ?? [];
        return {
            status,
            principal,
            permissions,
            sessionMode,
            can: (permission) => hasPermission(permissions, permission),
            canView: (view) => localSession
                ? canAccessLocalNamespaceView(view)
                : canAccessView(permissions, view),
            login,
            logout,
            refresh,
        };
    }, [status, principal, sessionMode, localSession, login, logout, refresh]);

    return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
