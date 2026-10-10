import React from 'react'
import { clsx } from 'clsx'

export const Badge = ({
  children, variant = 'neutral'
}: { children: React.ReactNode; variant?: 'buy' | 'sell' | 'neutral' | 'warning' | 'paper' | 'live' }) => {
  const cls = {
    buy:     'bg-emerald-950/50 text-emerald-400 border-emerald-900/60',
    sell:    'bg-rose-950/50 text-rose-400 border-rose-900/60',
    neutral: 'bg-slate-800/60 text-slate-400 border-slate-700/60',
    warning: 'bg-amber-950/40 text-amber-400 border-amber-900/50',
    paper:   'bg-amber-950/40 text-amber-400 border-amber-900/50',
    live:    'bg-rose-950/50 text-rose-400 border-rose-900/60',
  }[variant]
  return (
    <span className={clsx('inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold font-mono tracking-wide border', cls)}>
      {children}
    </span>
  )
}

export const Btn = ({
  children, onClick, variant = 'primary', size = 'md', disabled, className, type = 'button'
}: {
  children: React.ReactNode
  onClick?: () => void
  variant?: 'primary' | 'buy' | 'sell' | 'danger' | 'ghost' | 'outline'
  size?: 'sm' | 'md' | 'lg'
  disabled?: boolean
  className?: string
  type?: 'button' | 'submit'
}) => {
  const variants = {
    primary: 'bg-emerald-700 hover:bg-emerald-600 text-white border border-emerald-600/40',
    buy:     'bg-emerald-700 hover:bg-emerald-600 text-white border border-emerald-600/40',
    sell:    'bg-rose-800 hover:bg-rose-700 text-white border border-rose-700/40',
    danger:  'bg-rose-800 hover:bg-rose-700 text-white border border-rose-700/40',
    ghost:   'bg-transparent hover:bg-slate-800 text-slate-400 border border-transparent',
    outline: 'border border-slate-700 hover:bg-slate-800/80 text-slate-300 bg-transparent',
  }[variant]
  const sizes = {
    sm: 'px-2 py-1 text-[11px]',
    md: 'px-3 py-1.5 text-xs',
    lg: 'px-4 py-2 text-sm',
  }[size]
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        'rounded font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600/50 focus-visible:ring-offset-1 focus-visible:ring-offset-[#0a0c10]',
        variants, sizes,
        disabled && 'opacity-40 cursor-not-allowed',
        className,
      )}
    >
      {children}
    </button>
  )
}

export const Card = ({ children, className }: { children: React.ReactNode; className?: string }) => (
  <div className={clsx('bg-[#11141a] rounded border border-[#1e2430]', className)}>
    {children}
  </div>
)

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={clsx(
        'w-full border border-slate-700 rounded bg-slate-900 px-3 py-2 text-sm text-slate-200 font-mono',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600/50 focus:border-emerald-700',
        'placeholder:text-slate-600',
        className,
      )}
      {...props}
    />
  )
)
Input.displayName = 'Input'

export const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  ({ children, className, ...props }, ref) => (
    <select
      ref={ref}
      className={clsx(
        'w-full border border-slate-700 rounded bg-slate-900 px-3 py-2 text-sm text-slate-200',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600/50',
        'cursor-pointer',
        className,
      )}
      {...props}
    >
      {children}
    </select>
  )
)
Select.displayName = 'Select'

export const Pnl = ({ value }: { value: number }) => (
  <span className={clsx('font-mono text-xs font-semibold tabular-nums', value >= 0 ? 'text-emerald-400' : 'text-rose-400')}>
    {value >= 0 ? '+' : ''}₹{value.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
  </span>
)

export const Ltp = ({ value, prev }: { value: number; prev?: number }) => {
  const dir = prev === undefined ? '' : value > prev ? 'text-emerald-400' : value < prev ? 'text-rose-400' : 'text-slate-200'
  return (
    <span className={clsx('font-mono font-semibold tabular-nums', dir || 'text-slate-200')}>
      ₹{value.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
    </span>
  )
}

export const StatusDot = ({ online }: { online: boolean }) => (
  <span className={clsx('inline-block w-1.5 h-1.5 rounded-full', online ? 'bg-emerald-500' : 'bg-slate-600')} />
)

export const Modal = ({
  open, onClose, title, children
}: { open: boolean; onClose: () => void; title: string; children: React.ReactNode }) => {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/70" onClick={onClose} />
      <div className="relative bg-[#11141a] border border-[#1e2430] rounded-lg shadow-2xl p-6 w-full max-w-md mx-4">
        <h2 className="text-sm font-semibold text-slate-100 mb-4 tracking-wide">{title}</h2>
        {children}
      </div>
    </div>
  )
}
