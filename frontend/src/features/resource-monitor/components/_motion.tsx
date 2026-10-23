// frontend/src/features/resource-monitor/components/_motion.tsx
'use client';

/**
 * Shared animation primitives for the Resource Monitor.
 * Centralizes variants and animated-number/gauge components so every
 * card, table, and banner has consistent motion language.
 */

import { useEffect, useRef } from 'react';
import {
  animate,
  motion,
  useMotionValue,
  useReducedMotion,
  useTransform,
  type Variants,
} from 'motion/react';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Variants
// ---------------------------------------------------------------------------

export const fadeUp: Variants = {
  hidden: { opacity: 0, y: 12 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.35, ease: [0.16, 1, 0.3, 1] } },
};

export const staggerContainer: Variants = {
  hidden: {},
  visible: { transition: { staggerChildren: 0.07, delayChildren: 0.05 } },
};

export const tabTransition = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -8 },
  transition: { duration: 0.22, ease: [0.16, 1, 0.3, 1] as const },
};

// ---------------------------------------------------------------------------
// AnimatedNumber
// ---------------------------------------------------------------------------

interface AnimatedNumberProps {
  value: number;
  format: (v: number) => string;
  className?: string;
  duration?: number;
}

export function AnimatedNumber({ value, format, className, duration = 0.9 }: AnimatedNumberProps) {
  const prefersReduced = useReducedMotion();
  const motionValue = useMotionValue(value);
  const display = useTransform(motionValue, (v) => format(v));

  useEffect(() => {
    if (prefersReduced) {
      motionValue.set(value);
      return;
    }
    const controls = animate(motionValue, value, {
      duration,
      ease: [0.16, 1, 0.3, 1],
    });
    return () => controls.stop();
  }, [value, motionValue, duration, prefersReduced]);

  return <motion.span className={className}>{display}</motion.span>;
}

// ---------------------------------------------------------------------------
// CircularGauge
// ---------------------------------------------------------------------------

interface CircularGaugeProps {
  value: number;
  max?: number;
  size?: number;
  strokeWidth?: number;
  color: string;
  trackColor?: string;
  className?: string;
  children?: React.ReactNode;
}

export function CircularGauge({
  value,
  max = 100,
  size = 72,
  strokeWidth = 6,
  color,
  trackColor = 'rgba(148, 163, 184, 0.18)',
  className,
  children,
}: CircularGaugeProps) {
  const prefersReduced = useReducedMotion();
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(max, value));
  const dashoffset = circumference * (1 - clamped / max);

  const motionValue = useMotionValue(prefersReduced ? dashoffset : circumference);
  const strokeDashoffset = useTransform(motionValue, (v) => v);

  useEffect(() => {
    if (prefersReduced) {
      motionValue.set(dashoffset);
      return;
    }
    const controls = animate(motionValue, dashoffset, {
      duration: 1.1,
      ease: [0.16, 1, 0.3, 1],
    });
    return () => controls.stop();
  }, [dashoffset, motionValue, prefersReduced]);

  return (
    <div
      className={cn('relative inline-flex items-center justify-center', className)}
      style={{ width: size, height: size }}
    >
      <svg width={size} height={size} className="-rotate-90">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={trackColor}
          strokeWidth={strokeWidth}
        />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={circumference}
          style={{ strokeDashoffset }}
        />
      </svg>
      <div className="absolute inset-0 flex items-center justify-center">{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shimmer block (skeleton)
// ---------------------------------------------------------------------------

export function Skeleton({ className }: { className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  return (
    <div
      ref={ref}
      className={cn(
        'relative overflow-hidden rounded-md bg-slate-200/60 dark:bg-slate-800/60',
        className,
      )}
    >
      <motion.div
        className="absolute inset-0 bg-gradient-to-r from-transparent via-white/40 to-transparent dark:via-white/10"
        initial={{ x: '-100%' }}
        animate={{ x: '100%' }}
        transition={{ duration: 1.4, repeat: Infinity, ease: 'linear' }}
      />
    </div>
  );
}
