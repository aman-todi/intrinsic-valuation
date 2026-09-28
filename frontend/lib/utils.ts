/** Tiny className joiner (shadcn's `cn` without the tailwind-merge dependency). */
export function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}
