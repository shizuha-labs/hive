import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { ThemeProvider } from './hooks/useTheme'
import App from './App'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Agent fleet state is live-ish; keep it fresh but not chatty.
      staleTime: 1000 * 10,
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
})

// Base path from Vite (e.g. /hive/) so react-router matches behind nginx.
const basePath = import.meta.env.BASE_URL || '/'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter basename={basePath}>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  </React.StrictMode>,
)
