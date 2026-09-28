"use client";

import { useParams } from "next/navigation";
import { RunView } from "@/components/run-view";

export default function RunPage() {
  const { id } = useParams<{ id: string }>();
  return <RunView key={id} id={id} />;
}
