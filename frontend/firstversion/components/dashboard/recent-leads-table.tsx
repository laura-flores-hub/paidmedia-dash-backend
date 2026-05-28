"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { recentLeads } from "@/lib/dashboard-data";
import { cn } from "@/lib/utils";

const getScoreColor = (score: number) => {
  if (score >= 85) return "text-chart-2";
  if (score >= 65) return "text-chart-3";
  return "text-chart-4";
};

const getStatusBadge = (status: string) => {
  const styles: Record<string, string> = {
    qualified: "bg-chart-2/20 text-chart-2",
    contacted: "bg-chart-1/20 text-chart-1",
    new: "bg-chart-3/20 text-chart-3",
  };
  return styles[status] || "bg-secondary text-muted-foreground";
};

export function RecentLeadsTable() {
  return (
    <Card className="border-border/50">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">Recent Leads</CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        <Table>
          <TableHeader>
            <TableRow className="border-border/50 hover:bg-transparent">
              <TableHead className="text-xs text-muted-foreground font-medium">Name</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium">Company</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium">Source</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">Score</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">Value</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {recentLeads.map((lead) => (
              <TableRow key={lead.id} className="border-border/50">
                <TableCell>
                  <div>
                    <p className="text-sm font-medium">{lead.name}</p>
                    <p className="text-xs text-muted-foreground">{lead.email}</p>
                  </div>
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">{lead.company}</TableCell>
                <TableCell className="text-sm text-muted-foreground">{lead.source}</TableCell>
                <TableCell className="text-right">
                  <span className={cn("text-sm font-medium", getScoreColor(lead.score))}>
                    {lead.score}
                  </span>
                </TableCell>
                <TableCell className="text-sm text-right">{lead.value}</TableCell>
                <TableCell className="text-right">
                  <Badge variant="secondary" className={cn("text-[10px] uppercase", getStatusBadge(lead.status))}>
                    {lead.status}
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
