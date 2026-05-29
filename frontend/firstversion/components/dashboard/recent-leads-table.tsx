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
import type { Lead } from "@/lib/dashboard-data";
import { cn } from "@/lib/utils";

const getStatusBadge = (status: string) => {
  const styles: Record<string, string> = {
    salesqualifiedlead: "bg-chart-2/20 text-chart-2",
    qualified: "bg-chart-2/20 text-chart-2",
    opportunity: "bg-chart-1/20 text-chart-1",
    contacted: "bg-chart-1/20 text-chart-1",
    new: "bg-chart-3/20 text-chart-3",
    lead: "bg-chart-3/20 text-chart-3",
  };
  return styles[status] || "bg-secondary text-muted-foreground";
};

export function RecentLeadsTable({ leads }: { leads: Lead[] }) {
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
              <TableHead className="text-xs text-muted-foreground font-medium text-right">Date</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {leads.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-sm text-muted-foreground py-6">
                  No leads for this period.
                </TableCell>
              </TableRow>
            )}
            {leads.map((lead) => (
              <TableRow key={lead.id} className="border-border/50">
                <TableCell>
                  <div>
                    <p className="text-sm font-medium">{lead.name}</p>
                    <p className="text-xs text-muted-foreground">{lead.email}</p>
                  </div>
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">{lead.company}</TableCell>
                <TableCell className="text-sm text-muted-foreground">{lead.source}</TableCell>
                <TableCell className="text-sm text-right text-muted-foreground">{lead.date}</TableCell>
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
