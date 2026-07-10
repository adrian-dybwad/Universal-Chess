import type { ReactNode } from 'react';

type BadgeVariant = 'default' | 'success' | 'danger' | 'primary' | 'warning';

interface BadgeProps {
  variant?: BadgeVariant;
  children: ReactNode;
}

const variantClasses: Record<BadgeVariant, string> = {
  default: 'badge',
  success: 'badge badge--success',
  danger: 'badge badge--danger',
  primary: 'badge badge--primary',
  warning: 'badge badge--warning',
};

/**
 * Reusable badge/tag component for status indicators.
 */
export function Badge({ variant = 'default', children }: BadgeProps) {
  return <span className={variantClasses[variant]}>{children}</span>;
}

