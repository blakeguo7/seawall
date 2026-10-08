import React, {createContext, useContext} from 'react';

import {type ThemeConfig, defaultTheme} from './builtinThemes.js';

export type {ThemeConfig};

type ThemeContextValue = {
	theme: ThemeConfig;
};

const ThemeContext = createContext<ThemeContextValue>({theme: defaultTheme});

export function ThemeProvider({children}: {children: React.ReactNode}): React.JSX.Element {
	return <ThemeContext.Provider value={{theme: defaultTheme}}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
	return useContext(ThemeContext);
}
