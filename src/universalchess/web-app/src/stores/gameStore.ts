import { create } from 'zustand';
import type { GameState, AnalysisResult, ConnectionStatus, BatteryStatus } from '../types/game';

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
  analysis: AnalysisResult | null;
  analysisHistory: AnalysisResult[];
  currentMoveIndex: number;
  toast: MoveToastData | null;
  
  setGameState: (state: GameState) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  setBattery: (battery: BatteryStatus) => void;
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
  analysis: null,
  analysisHistory: [],
  currentMoveIndex: -1,
  toast: null,

  setGameState: (gameState) => set({ gameState }),
  setConnectionStatus: (connectionStatus) => set({ connectionStatus }),
  setBattery: (battery) => set({ battery }),
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
