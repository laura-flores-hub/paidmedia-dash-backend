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
import { campaignData } from "@/lib/dashboard-data";
import { cn } from "@/lib/utils";

export function CampaignsTable() {
  return (
    <Card className="border-border/50">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">Active Campaigns</CardTitle>
      </CardHeader>
      <CardContent className="pt-0">
        <Table>
          <TableHeader>
            <TableRow className="border-border/50 hover:bg-transparent">
              <TableHead className="text-xs text-muted-foreground font-medium">Campaign</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium">Platform</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">Spend</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">Leads</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">CPL</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">ROAS</TableHead>
              <TableHead className="text-xs text-muted-foreground font-medium text-right">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {campaignData.campaigns.map((campaign) => (
              <TableRow key={campaign.name} className="border-border/50">
                <TableCell className="text-sm font-medium">{campaign.name}</TableCell>
                <TableCell className="text-sm text-muted-foreground">{campaign.platform}</TableCell>
                <TableCell className="text-sm text-right">${campaign.spend.toLocaleString()}</TableCell>
                <TableCell className="text-sm text-right">{campaign.leads}</TableCell>
                <TableCell className="text-sm text-right">
                  <span
                    className={cn(
                      campaign.cpl > 80 ? "text-chart-4" : campaign.cpl < 50 ? "text-chart-2" : ""
                    )}
                  >
                    ${campaign.cpl.toFixed(2)}
                  </span>
                </TableCell>
                <TableCell className="text-sm text-right">
                  <span
                    className={cn(
                      campaign.roas >= 3 ? "text-chart-2" : campaign.roas < 2 ? "text-chart-4" : ""
                    )}
                  >
                    {campaign.roas.toFixed(2)}x
                  </span>
                </TableCell>
                <TableCell className="text-right">
                  <Badge
                    variant="secondary"
                    className={cn(
                      "text-[10px] uppercase",
                      campaign.status === "active"
                        ? "bg-chart-2/20 text-chart-2"
                        : "bg-chart-3/20 text-chart-3"
                    )}
                  >
                    {campaign.status}
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
