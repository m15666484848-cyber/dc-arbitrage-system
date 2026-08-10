import { create } from "zustand";

interface AccountFilterState {
  accountId: number | null;
  setAccountId: (id: number | null) => void;
}

export const useAccountFilterStore = create<AccountFilterState>((set) => ({
  accountId: null,
  setAccountId: (id) => set({ accountId: id }),
}));
