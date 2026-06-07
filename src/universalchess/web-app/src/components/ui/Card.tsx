import type { HTMLAttributes, ReactNode } from 'react';

type CardVariant = 'default' | 'muted' | 'primary' | 'danger' | 'success';

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  variant?: CardVariant;
  children: ReactNode;
}

const VARIANT_CLASSES: Record<CardVariant, string> = {
  default: '',
  muted: 'card--muted',
  primary: 'card--primary',
  danger: 'card--danger',
  success: 'card--success',
};

/**
 * Reusable card container component.
 */
export function Card({
  variant = 'default',
  className = '',
  children,
  ...props
}: CardProps) {
  const classes = [
    'card',
    VARIANT_CLASSES[variant],
    className,
  ].filter(Boolean).join(' ');

  return (
    <div className={classes} {...props}>
      {children}
    </div>
  );
}

interface CardHeaderProps {
  title: string;
  action?: ReactNode;
}

/**
 * Card header with title, optional action, and horizontal rule.
 */
export function CardHeader({ title, action }: CardHeaderProps) {
  return (
    <>
      <div className="card-header">
        <h3 className="card-title">{title}</h3>
        {action}
      </div>
      <hr className="card-divider" />
    </>
  );
}

