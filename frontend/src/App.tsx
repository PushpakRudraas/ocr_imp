import { Link, Route, Routes } from 'react-router-dom';
import RunPage from './components/RunPage';
import UploadPage from './components/UploadPage';

export default function App() {
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/" className="brand">
          <span className="brand-mark">N</span> Newspaper Intelligence
        </Link>
        <nav>
          <Link to="/">Upload</Link>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<UploadPage />} />
          <Route path="/runs/:runId" element={<RunPage />} />
          <Route path="*" element={<p className="empty">Page not found. <Link to="/">Back to upload</Link></p>} />
        </Routes>
      </main>
    </div>
  );
}
