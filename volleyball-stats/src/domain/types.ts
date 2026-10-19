// Core data types. The event log per match is the source of truth; everything
// else (score, rotation, who is on court, stats) is derived by replaying it.

export type Gender = 'F' | 'M';
export type Position = 'S' | 'OH' | 'MB' | 'OPP' | 'L';

export interface Player {
  id: string;
  name: string;
  number: number;
  gender?: Gender;
  positions: Position[];
  active: boolean;
}

export interface Season {
  id: string;
  name: string;
  createdAt: number;
}

export interface RulesPreset {
  name: string;
  setPoints: number;
  /** A set ends when a team reaches this score, even with a 1-point lead. null = no cap. */
  setCap: number | null;
  tiebreakPoints: number;
  tiebreakCap: number | null;
  winBy: number;
  bestOf: number;
  timeoutsPerSet: number;
  subsPerSet: number;
  /** Required players of each gender on court (libero exempt). null = no gender rule. */
  genderParity: { F: number; M: number } | null;
  maxLiberos: number;
  /** League points for us, keyed by "<our sets>-<their sets>". */
  leaguePoints: Record<string, number>;
}

/** Skills, by their code letter. */
export type Skill = 'S' | 'R' | 'A' | 'B' | 'D' | 'E' | 'F';
export const SKILLS: Skill[] = ['S', 'R', 'A', 'B', 'D', 'E', 'F'];

/** How much of a skill is recorded: not at all, only errors, or every outcome. */
export type SkillMode = 'off' | 'errors' | 'all';

export type TrackingProfile = 'score' | 'basic' | 'full' | 'custom';

export interface Tracking {
  profile: TrackingProfile;
  skills: Record<Skill, SkillMode>;
  /** Record every set (E+) instead of only set errors. */
  recordEverySet: boolean;
  /** Credit kills' assists to the setter on court. */
  inferAssists: boolean;
  /** Gender, substitution and libero rule warnings. */
  ruleChecks: boolean;
}

/** Outcome symbols: # point/ace/kill, + in play, / blocked, = error. */
export type Outcome = '#' | '+' | '/' | '=';

export interface Match {
  id: string;
  seasonId: string;
  date: string; // YYYY-MM-DD
  opponent: string;
  home: boolean;
  competition: string;
  rules: RulesPreset;
  tracking: Tracking;
  /** false = friendly / practice: excluded from season stats. */
  countsInSeason: boolean;
  /** Locked against accidental edits. */
  final: boolean;
  /** The side that forfeited (did not show up): recorded as a 3–0 result. */
  forfeit: 'us' | 'them' | null;
  /** Set numbers excluded from stats. */
  excludedSets: number[];
  createdAt: number;
}

export type EventBody =
  | {
      type: 'set_start';
      set: number;
      /** Player ids by zone: index 0 = zone 1 … index 5 = zone 6. */
      lineup: string[];
      setterId: string | null;
      liberos: string[];
      /** Players the libero automatically replaces in the back row (usually the middles). */
      liberoReplaces: string[];
      weServe: boolean;
    }
  | {
      type: 'action';
      /** Missing = team action, no player credited (or the server, for S). */
      playerId: string | null;
      skill: Skill;
      outcome: Outcome | null;
      /** Reception rating 0–3, null = unrated. */
      rating: number | null;
      /** Other blockers on a shared block point. */
      coBlockers?: string[];
    }
  | { type: 'opp_point' }
  /** Our point with nobody credited (e.g. score-only tracking). */
  | { type: 'our_point' }
  | { type: 'opp_error' }
  | { type: 'penalty' }
  | { type: 'timeout' }
  | { type: 'sub'; inId: string; outId: string; injury: boolean }
  /** Libero override: liberoId null = toggle automatic libero on/off; otherwise make that libero the active one. */
  | { type: 'libero'; liberoId: string | null }
  | { type: 'tracking'; tracking: Tracking }
  | { type: 'correction'; us: number; them: number; weServe: boolean; rotations: number };

export type MatchEvent = EventBody & {
  id: string;
  matchId: string;
  /** Sort key; fractional so events can be inserted between others. */
  order: number;
  createdAt: number;
};
