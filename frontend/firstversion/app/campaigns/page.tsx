import { DollarSign, Users, TrendingUp, Target } from "lucide-react";
import { Sidebar } from "@/components/dashboard/sidebar";
import { ThemeToggle } from "@/components/theme-toggle";
import { KPICard } from "@/components/dashboard/kpi-card";
import { PlatformCard } from "@/components/campaigns/platform-card";
import { PlatformComparison } from "@/components/campaigns/platform-comparison";
import { platformData } from "@/lib/dashboard-data";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// Platform icons as simple SVG components
function MetaIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2.04c-5.5 0-10 4.49-10 10.02 0 5 3.66 9.15 8.44 9.9v-7H7.9v-2.9h2.54V9.85c0-2.51 1.49-3.89 3.78-3.89 1.09 0 2.23.19 2.23.19v2.47h-1.26c-1.24 0-1.63.77-1.63 1.56v1.88h2.78l-.45 2.9h-2.33v7a10 10 0 0 0 8.44-9.9c0-5.53-4.5-10.02-10-10.02Z" />
    </svg>
  );
}

function GoogleIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
    </svg>
  );
}

function LinkedInIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" />
    </svg>
  );
}

export default function CampaignsPage() {
  const totalSpend =
    platformData.meta.totalSpend +
    platformData.google.totalSpend +
    platformData.linkedin.totalSpend;
  const totalRevenue =
    platformData.meta.totalRevenue +
    platformData.google.totalRevenue +
    platformData.linkedin.totalRevenue;
  const totalLeads =
    platformData.meta.leads + platformData.google.leads + platformData.linkedin.leads;
  const avgRoas = totalRevenue / totalSpend;

  const platformSummaries = [
    {
      name: platformData.meta.name,
      spend: platformData.meta.totalSpend,
      leads: platformData.meta.leads,
      cpl: platformData.meta.cpl,
      roas: platformData.meta.roas,
      color: "hsl(var(--chart-1))",
    },
    {
      name: platformData.google.name,
      spend: platformData.google.totalSpend,
      leads: platformData.google.leads,
      cpl: platformData.google.cpl,
      roas: platformData.google.roas,
      color: "hsl(var(--chart-2))",
    },
    {
      name: platformData.linkedin.name,
      spend: platformData.linkedin.totalSpend,
      leads: platformData.linkedin.leads,
      cpl: platformData.linkedin.cpl,
      roas: platformData.linkedin.roas,
      color: "hsl(var(--chart-5))",
    },
  ];

  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar />

      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <header className="border-b border-border/50 bg-card/50">
          <div className="px-4 py-4 sm:px-6 lg:px-8">
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h1 className="text-xl font-semibold tracking-tight">
                  Paid Campaigns
                </h1>
                <p className="text-sm text-muted-foreground mt-1">
                  Performance breakdown by platform: Meta, Google, and LinkedIn
                </p>
              </div>
              <div className="flex items-center gap-3">
                <Select defaultValue="30d">
                  <SelectTrigger className="w-[140px] h-9 text-sm bg-secondary border-border/50">
                    <SelectValue placeholder="Select period" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="7d">Last 7 days</SelectItem>
                    <SelectItem value="30d">Last 30 days</SelectItem>
                    <SelectItem value="90d">Last 90 days</SelectItem>
                    <SelectItem value="ytd">Year to date</SelectItem>
                  </SelectContent>
                </Select>
                <ThemeToggle />
              </div>
            </div>
          </div>
        </header>

        {/* Main Content */}
        <main className="flex-1 overflow-auto px-4 py-6 sm:px-6 lg:px-8 space-y-6">
          {/* Overall KPIs */}
          <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <KPICard
              title="Total Ad Spend"
              value={`$${(totalSpend / 1000).toFixed(1)}K`}
              subtitle="Across all platforms"
              trend={8.2}
              trendLabel="vs last month"
              icon={<DollarSign className="h-4 w-4" />}
            />
            <KPICard
              title="Total Leads"
              value={totalLeads.toLocaleString()}
              subtitle="From paid campaigns"
              trend={12.5}
              trendLabel="vs last month"
              icon={<Users className="h-4 w-4" />}
            />
            <KPICard
              title="Avg. CPL"
              value={`$${(totalSpend / totalLeads).toFixed(2)}`}
              subtitle="Cost per lead"
              trend={-5.3}
              trendLabel="vs last month"
              icon={<Target className="h-4 w-4" />}
            />
            <KPICard
              title="Overall ROAS"
              value={`${avgRoas.toFixed(2)}x`}
              subtitle="Return on ad spend"
              trend={4.1}
              trendLabel="vs last month"
              icon={<TrendingUp className="h-4 w-4" />}
            />
          </section>

          {/* Platform Comparison Charts */}
          <section>
            <h2 className="text-sm font-medium mb-4">Platform Comparison</h2>
            <PlatformComparison platforms={platformSummaries} />
          </section>

          {/* Meta Ads Section */}
          <section>
            <PlatformCard
              platform={platformData.meta}
              icon={<MetaIcon className="h-5 w-5 text-primary" />}
            />
          </section>

          {/* Google Ads Section */}
          <section>
            <PlatformCard
              platform={platformData.google}
              icon={<GoogleIcon className="h-5 w-5 text-accent" />}
            />
          </section>

          {/* LinkedIn Ads Section */}
          <section>
            <PlatformCard
              platform={platformData.linkedin}
              icon={<LinkedInIcon className="h-5 w-5 text-chart-5" />}
            />
          </section>
        </main>
      </div>
    </div>
  );
}
