import { Routes, Route, Navigate } from 'react-router-dom'
import { HiveProvider } from './contexts/HiveContext'
import { ToastProvider } from './components/Toast'
import Layout from './components/Layout'
import Overview from './pages/Overview'
import Agents from './pages/Agents'
import InferenceKey from './pages/InferenceKey'
import TokenPools from './pages/TokenPools'
import OrgProviders from './pages/OrgProviders'
import CredentialHealth from './pages/CredentialHealth'
import Waitlist from './pages/Waitlist'
import Teams from './pages/Teams'
import Credentials from './pages/Credentials'
import CreateOrganization from './pages/CreateOrganization'

function App() {
  return (
    <ToastProvider>
      <HiveProvider>
        <Routes>
          {/* Public landing page — no auth required. Redirects to /overview when signed in. */}
          <Route index element={<Waitlist />} />

          {/* Authenticated app shell — pathless layout wrapper covers all sub-paths */}
          <Route element={<Layout />}>
            <Route path="overview" element={<Overview />} />
            <Route path="create-organization" element={<CreateOrganization />} />
            <Route path="agents" element={<Agents />} />
            {/* legacy paths -> unified agents surface (keep bookmarks working) */}
            <Route path="fleet" element={<Navigate to="/agents" replace />} />
            <Route path="my-agents" element={<Navigate to="/agents" replace />} />
            <Route path="claws" element={<Navigate to="/agents" replace />} />
            <Route path="teams" element={<Teams />} />
            <Route path="credentials" element={<Credentials />} />
            <Route path="credentials/codex/re-auth" element={<TokenPools />} />
            <Route path="inference-key" element={<InferenceKey />} />
            <Route path="token-pools" element={<TokenPools />} />
            <Route path="settings/providers" element={<OrgProviders />} />
            <Route path="subscriptions" element={<CredentialHealth />} />
            <Route path="credential-health" element={<CredentialHealth />} />
            <Route path="*" element={<Navigate to="/overview" replace />} />
          </Route>
        </Routes>
      </HiveProvider>
    </ToastProvider>
  )
}

export default App
