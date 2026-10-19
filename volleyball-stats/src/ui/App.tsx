import { useSetting } from './hooks';
import { useRoute } from './router';
import { MatchesPage } from './pages/MatchesPage';
import { TeamPage } from './pages/TeamPage';
import { MatchSetupPage } from './pages/MatchSetupPage';
import { LivePage } from './pages/LivePage';
import { EditPage } from './pages/EditPage';
import { ReportPage } from './pages/ReportPage';
import { SeasonPage } from './pages/SeasonPage';
import { DataPage } from './pages/DataPage';

const NAV = [
  ['', 'Matches'],
  ['season', 'Season'],
  ['team', 'Team'],
  ['data', 'Data & help'],
] as const;

export function App() {
  const route = useRoute();
  const teamName = useSetting('teamName', 'My team');
  const section = route[0] ?? '';

  let page;
  if (section === 'team') page = <TeamPage />;
  else if (section === 'season') page = <SeasonPage />;
  else if (section === 'data') page = <DataPage />;
  else if (section === 'match' && route[1] === 'new') page = <MatchSetupPage matchId={null} />;
  else if (section === 'match' && route[1]) {
    const id = route[1];
    const view = route[2];
    if (view === 'setup') page = <MatchSetupPage matchId={id} />;
    else if (view === 'edit') page = <EditPage matchId={id} />;
    else if (view === 'report') page = <ReportPage matchId={id} />;
    else page = <LivePage matchId={id} />;
  } else page = <MatchesPage />;

  return (
    <>
      <header className="topbar">
        <a className="brand" href="#/">
          <img src="./icon.svg" alt="" />
          {teamName}
        </a>
        <nav>
          {NAV.map(([path, label]) => (
            <a key={path} href={`#/${path}`} className={section === path || (path === '' && section === 'match') ? 'active' : ''}>
              {label}
            </a>
          ))}
        </nav>
      </header>
      {page}
    </>
  );
}
