import React, { useState, useEffect } from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { LoginPage } from './pages/LoginPage'
import { KlinePage } from './pages/KlinePage'
import { AnalysisPage } from './pages/AnalysisPage'
import { StockDetailPage } from './pages/StockDetailPage'
import './index.css'

const TOKEN_KEY = 'market-lab-token'

const RequireAuth: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [isAuthenticated, setIsAuthenticated] = useState<boolean | null>(null)

  useEffect(() => {
    const token = localStorage.getItem(TOKEN_KEY)
    setIsAuthenticated(!!token)
  }, [])

  if (isAuthenticated === null) {
    return <div style={{ textAlign: 'center', padding: '100px' }}>加载中...</div>
  }

  return isAuthenticated ? <>{children}</> : <Navigate to="/ui/login" replace />
}

const App: React.FC = () => {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/ui/login" element={<LoginPage />} />
        <Route path="/ui" element={
          <RequireAuth>
            <KlinePage />
          </RequireAuth>
        } />
        <Route path="/ui/analysis" element={
          <RequireAuth>
            <AnalysisPage />
          </RequireAuth>
        } />
        <Route path="/ui/stock/:code" element={
          <RequireAuth>
            <StockDetailPage />
          </RequireAuth>
        } />
        <Route path="/ui/*" element={
          <RequireAuth>
            <KlinePage />
          </RequireAuth>
        } />
        <Route path="*" element={<Navigate to="/ui" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
