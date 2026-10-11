// frontend/src/components/generative-ui/catalog/PeldrunMotionWrapper.tsx
"use client";

import React from "react";
import { motion } from "motion/react";

interface PeldrunMotionWrapperProps {
  children: React.ReactNode;
  delay?: number;
  className?: string;
}

export function PeldrunMotionWrapper({
  children,
  delay = 0,
  className = "",
}: PeldrunMotionWrapperProps): React.JSX.Element {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10, scale: 0.99 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{
        duration: 0.35,
        delay,
        ease: [0.16, 1, 0.3, 1],
      }}
      className={className}
    >
      {children}
    </motion.div>
  );
}
