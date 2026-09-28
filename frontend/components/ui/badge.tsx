import * as React from "react";
import { cn } from "@/lib/utils";

type Variant = "default" | "secondary" | "outline" | "positive" | "negative" | "warning";

const variants: Record<Variant, string> = {
  default: "bg-primary text-primary-foreground border-transparent",
  secondary: "bg-secondary text-secondary-foreground border-transparent",
  outline: "text-foreground border-border",
  positive: "text-positive border-positive/40 bg-positive/10",
  negative: "text-negative border-negative/40 bg-negative/10",
  warning: "text-warning border-warning/40 bg-warning-bg",
};

export function Badge({
  className,
  variant = "secondary",
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { variant?: Variant }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium",
        variants[variant],
        className,
      )}
      {...props}
    />
  );
}
