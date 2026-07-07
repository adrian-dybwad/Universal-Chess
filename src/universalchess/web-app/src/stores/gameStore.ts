import { create } from 'zustand';
import type { GameState, AnalysisResult, ConnectionStatus, BatteryStatus, ClockStatus } from '../types/game';

// Persist the board hostname so the browser tab title is device-prefixed on the
// very first paint of every load after the first, instead of flashing the bare
// page name until /api/system/stats responds. The cached value is refreshed (and
// corrected, e.g. after pointing the PWA at a different board) once stats is read.
const DEVICE_NAME_KEY = 'universal-chess-device-name';

function loadStoredDeviceName(): string | null {
  const stored = localStorage.getItem(DEVICE_NAME_KEY);
  return stored && stored.trim() ? stored.trim() : null;
}

export interface MoveToastData {
  move: string;
  moveNumber: number;
  white: string;
  black: string;
  isWhiteMove: boolean;
}

interface GameStoreState {
  gameState: GameState | null;
  connectionStatus: ConnectionStatus;
  battery: BatteryStatus | null;
  clock: ClockStatus | null;
  // The board's hostname (e.g. "dgt"), used to prefix the browser tab title so
  // multiple boards open in separate tabs are distinguishable. Null until read
  // from /api/system/stats.
  deviceName: string | null;
  analysis: AnalysisResult | null;
  analysisHistory: AnalysisResult[];
  currentMoveIndex: number;
  toast: MoveToastData | null;
  
  setGameState: (state: GameState) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  setBattery: (battery: BatteryStatus) => void;
  setClock: (clock: ClockStatus) => void;
  setDeviceName: (deviceName: string) => void;
  setAnalysis: (analysis: AnalysisResult) => void;
  addAnalysisToHistory: (analysis: AnalysisResult) => void;
  setCurrentMoveIndex: (index: number) => void;
  clearAnalysisHistory: () => void;
  showToast: (data: MoveToastData) => void;
  hideToast: () => void;
}

export const useGameStore = create<GameStoreState>((set) => ({
  gameState: null,
  connectionStatus: 'disconnected',
  battery: null,
  clock: null,
  deviceName: loadStoredDeviceName(),
  analysis: null,
  analysisHistory: [],
  currentMoveIndex: -1,
  toast: null,

  setGameState: (gameState) => set({ gameState }),
  setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
  setBattery: (battery) => set({ battery }),
  setClock: (clock) => set({ clock }),
  setDeviceName: (deviceName) => {
    localStorage.setItem(DEVICE_NAME_KEY, deviceName);
    set({ deviceName });
  },
  setAnalysis: (analysis) => set({ analysis }),
  addAnalysisToHistory: (analysis) =>
    set((state) => ({
      analysisHistory: [...state.analysisHistory, analysis],
    })),
  setCurrentMoveIndex: (currentMoveIndex) => set({ currentMoveIndex }),
  clearAnalysisHistory: () => set({ analysisHistory: [], currentMoveIndex: -1 }),
  showToast: (data) => set({ toast: data }),
  hideToast: () => set({ toast: null }),
}));
