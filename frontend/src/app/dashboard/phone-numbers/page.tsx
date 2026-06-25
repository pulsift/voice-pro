"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Phone, Plus, MoreVertical, Search, Loader2, RefreshCw, FolderOpen } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { toast } from "sonner";
import { searchPhoneNumbers, purchasePhoneNumber, type Provider } from "@/lib/api/telephony";
import {
  listPhoneNumbers as listRegistryPhoneNumbers,
  deletePhoneNumber,
  type PhoneNumber as RegistryPhoneNumber,
} from "@/lib/api/phone-numbers";
import { fetchAgents, updateAgent, type Agent } from "@/lib/api/agents";
import { api } from "@/lib/api";

interface Workspace {
  id: string;
  name: string;
  description: string | null;
  is_default: boolean;
}

type PhoneNumber = {
  id: string;
  phoneNumber: string;
  provider: string;
  agentId?: string;
  agentName?: string;
  workspaceId?: string;
  workspaceName?: string;
  isActive: boolean;
};

type AvailableNumber = {
  phone_number: string;
  friendly_name?: string | null;
};

export default function PhoneNumbersPage() {
  // State for phone numbers
  const [phoneNumbers, setPhoneNumbers] = useState<PhoneNumber[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWorkspaceId, setSelectedWorkspaceId] = useState<string>("all");
  const [isLoading, setIsLoading] = useState(true);

  // State for modals
  const [isPurchaseModalOpen, setIsPurchaseModalOpen] = useState(false);
  const [isDetailsModalOpen, setIsDetailsModalOpen] = useState(false);
  const [isAssignModalOpen, setIsAssignModalOpen] = useState(false);
  const [selectedNumber, setSelectedNumber] = useState<PhoneNumber | null>(null);

  // Purchase flow state
  const [searchAreaCode, setSearchAreaCode] = useState("");
  const [selectedProvider, setSelectedProvider] = useState<Provider>("telnyx");
  const [isSearching, setIsSearching] = useState(false);
  const [availableNumbers, setAvailableNumbers] = useState<AvailableNumber[]>([]);
  const [selectedAvailableNumber, setSelectedAvailableNumber] = useState<string | null>(null);
  const [isPurchasing, setIsPurchasing] = useState(false);
  const [isReleaseDialogOpen, setIsReleaseDialogOpen] = useState(false);
  const [numberToRelease, setNumberToRelease] = useState<PhoneNumber | null>(null);
  const [isAssigning, setIsAssigning] = useState(false);

  // Load phone numbers and agents on mount
  useEffect(() => {
    void loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Get the active workspace ID for API calls
  const getActiveWorkspaceId = (): string | null => {
    if (selectedWorkspaceId !== "all") {
      return selectedWorkspaceId;
    }
    // Use default workspace or first workspace
    const defaultWs = workspaces.find((ws) => ws.is_default);
    return defaultWs?.id ?? workspaces[0]?.id ?? null;
  };

  const loadData = async () => {
    setIsLoading(true);
    try {
      // First load workspaces and agents
      const [workspacesResult, agentsList] = await Promise.allSettled([
        api.get("/api/v1/workspaces"),
        fetchAgents(),
      ]);

      // Set workspaces
      let loadedWorkspaces: Workspace[] = [];
      if (workspacesResult.status === "fulfilled") {
        loadedWorkspaces = workspacesResult.value.data as Workspace[];
        setWorkspaces(loadedWorkspaces);
      }

      // Get agents list
      const agentsData = agentsList.status === "fulfilled" ? agentsList.value : [];
      setAgents(agentsData);

      // Load ALL registered numbers from the DB registry — the source of truth that
      // the top-bar count reads. Only scope by workspace when a specific one is picked;
      // "All Workspaces" returns everything, including numbers with no workspace.
      const registry = await listRegistryPhoneNumbers({
        page_size: 100,
        workspace_id: selectedWorkspaceId !== "all" ? selectedWorkspaceId : undefined,
      });

      const numbers: PhoneNumber[] = registry.phone_numbers.map((n: RegistryPhoneNumber) => ({
        id: n.id,
        phoneNumber: n.phone_number,
        provider: n.provider,
        agentId: n.assigned_agent_id ?? undefined,
        agentName: n.assigned_agent_name ?? undefined,
        workspaceId: n.workspace_id ?? undefined,
        workspaceName: n.workspace_name ?? undefined,
        isActive: n.status === "active",
      }));

      // Backfill agent names from the agents list where the registry didn't resolve one.
      const numbersWithAgents = numbers.map((num) => {
        if (num.agentId && !num.agentName) {
          const agent = agentsData.find((a: Agent) => a.id === num.agentId);
          return { ...num, agentName: agent?.name };
        }
        if (!num.agentId) {
          const assignedAgent = agentsData.find(
            (a: Agent) => a.phone_number_id === num.id || a.phone_number_id === num.phoneNumber
          );
          if (assignedAgent) {
            return { ...num, agentId: assignedAgent.id, agentName: assignedAgent.name };
          }
        }
        return num;
      });

      setPhoneNumbers(numbersWithAgents);
    } catch (error) {
      console.error("Failed to load data:", error);
      // Don't show error toast - credentials may not be configured
    } finally {
      setIsLoading(false);
    }
  };

  const openPurchaseModal = () => {
    setSearchAreaCode("");
    setAvailableNumbers([]);
    setSelectedAvailableNumber(null);
    setIsPurchaseModalOpen(true);
  };

  const searchAvailableNumbersHandler = async () => {
    if (!searchAreaCode || searchAreaCode.length < 3) {
      toast.error("Please enter at least 3 digits for area code");
      return;
    }

    const workspaceId = getActiveWorkspaceId();
    if (!workspaceId) {
      toast.error("No workspace available. Please create a workspace first.");
      return;
    }

    setIsSearching(true);
    try {
      const numbers = await searchPhoneNumbers(
        {
          provider: selectedProvider,
          area_code: searchAreaCode,
          limit: 10,
        },
        workspaceId
      );

      setAvailableNumbers(
        numbers.map((n) => ({
          phone_number: n.phone_number,
          friendly_name: n.friendly_name,
        }))
      );

      if (numbers.length === 0) {
        toast.info("No numbers found for this area code");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to search for numbers";
      toast.error(message);
    } finally {
      setIsSearching(false);
    }
  };

  const purchaseNumberHandler = async () => {
    if (!selectedAvailableNumber) {
      toast.error("Please select a number to purchase");
      return;
    }

    const workspaceId = getActiveWorkspaceId();
    if (!workspaceId) {
      toast.error("No workspace available. Please create a workspace first.");
      return;
    }

    setIsPurchasing(true);
    try {
      await purchasePhoneNumber(
        {
          provider: selectedProvider,
          phone_number: selectedAvailableNumber,
        },
        workspaceId
      );
      toast.success(`Successfully purchased ${selectedAvailableNumber}`);
      setIsPurchaseModalOpen(false);
      // Reload data to show new number
      await loadData();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to purchase number";
      toast.error(message);
    } finally {
      setIsPurchasing(false);
    }
  };

  const openDetailsModal = (number: PhoneNumber) => {
    setSelectedNumber(number);
    setIsDetailsModalOpen(true);
  };

  const openAssignModal = (number: PhoneNumber) => {
    setSelectedNumber(number);
    setIsAssignModalOpen(true);
  };

  const handleAssignAgent = async (agentId: string) => {
    if (!selectedNumber) return;

    setIsAssigning(true);
    try {
      // Update the agent with the phone number
      await updateAgent(agentId, {
        phone_number_id: selectedNumber.phoneNumber,
      });
      const agent = agents.find((a) => a.id === agentId);
      toast.success(`Assigned ${selectedNumber.phoneNumber} to ${agent?.name}`);
      setIsAssignModalOpen(false);
      // Reload to reflect changes
      await loadData();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to assign agent";
      toast.error(message);
    } finally {
      setIsAssigning(false);
    }
  };

  const handleReleaseNumber = (number: PhoneNumber) => {
    setNumberToRelease(number);
    setIsReleaseDialogOpen(true);
  };

  const confirmReleaseNumber = async () => {
    if (!numberToRelease) return;

    try {
      await deletePhoneNumber(numberToRelease.id);
      toast.success(`Removed ${numberToRelease.phoneNumber}`);
      // Reload to reflect changes
      await loadData();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to release number";
      toast.error(message);
    } finally {
      setIsReleaseDialogOpen(false);
      setNumberToRelease(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">Phone Numbers</h1>
          <p className="text-sm text-muted-foreground">
            Manage phone numbers for your voice agents
          </p>
        </div>
        <div className="flex gap-2">
          {workspaces.length > 0 && (
            <Select
              value={selectedWorkspaceId}
              onValueChange={(value) => {
                setSelectedWorkspaceId(value);
                const wsName =
                  value === "all"
                    ? "All Workspaces"
                    : workspaces.find((ws) => ws.id === value)?.name;
                toast.info(`Switched to ${wsName}`);
              }}
            >
              <SelectTrigger className="h-8 w-[220px] text-sm">
                <FolderOpen className="mr-2 h-3.5 w-3.5" />
                <SelectValue placeholder="All Workspaces" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All Workspaces (Admin)</SelectItem>
                {workspaces.map((ws) => (
                  <SelectItem key={ws.id} value={ws.id}>
                    {ws.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Button size="sm" variant="outline" onClick={() => void loadData()} disabled={isLoading}>
            <RefreshCw className={`mr-2 h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button size="sm" onClick={openPurchaseModal}>
            <Plus className="mr-2 h-4 w-4" />
            Purchase Number
          </Button>
        </div>
      </div>

      {isLoading ? (
        <Card>
          <CardContent className="flex items-center justify-center py-16">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </CardContent>
        </Card>
      ) : phoneNumbers.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center justify-center py-16">
            <Phone className="mb-4 h-16 w-16 text-muted-foreground/50" />
            <h3 className="mb-2 text-lg font-semibold">No phone numbers yet</h3>
            <p className="mb-4 max-w-sm text-center text-sm text-muted-foreground">
              Purchase a phone number from Telnyx or Twilio to start receiving calls. Make sure to
              configure your API credentials in Settings first.
            </p>
            <Button size="sm" onClick={openPurchaseModal}>
              <Plus className="mr-2 h-4 w-4" />
              Purchase Your First Number
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>Your Phone Numbers</CardTitle>
            <CardDescription>
              {phoneNumbers.length} number(s) available
              {selectedWorkspaceId !== "all" && (
                <span className="ml-1 text-muted-foreground">(workspace filter coming soon)</span>
              )}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Phone Number</TableHead>
                  <TableHead>Provider</TableHead>
                  <TableHead>Assigned Agent</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead className="w-[70px]"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {phoneNumbers.map((number) => (
                  <TableRow key={number.id}>
                    <TableCell className="font-mono font-medium">{number.phoneNumber}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="capitalize">
                        {number.provider}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {number.agentName ?? (
                        <span className="text-muted-foreground">Unassigned</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={number.isActive ? "default" : "secondary"}>
                        {number.isActive ? "Active" : "Inactive"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="ghost" size="icon">
                            <MoreVertical className="h-4 w-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem onClick={() => openAssignModal(number)}>
                            Assign to Agent
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => openDetailsModal(number)}>
                            View Details
                          </DropdownMenuItem>
                          <DropdownMenuItem
                            className="text-destructive"
                            onClick={() => handleReleaseNumber(number)}
                          >
                            Release Number
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* Purchase Number Modal */}
      <Dialog open={isPurchaseModalOpen} onOpenChange={setIsPurchaseModalOpen}>
        <DialogContent className="sm:max-w-[500px]">
          <DialogHeader>
            <DialogTitle>Purchase Phone Number</DialogTitle>
            <DialogDescription>Search for available phone numbers by area code</DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label htmlFor="provider">Provider</Label>
              <Select
                value={selectedProvider}
                onValueChange={(v) => setSelectedProvider(v as Provider)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Select provider" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="telnyx">Telnyx</SelectItem>
                  <SelectItem value="twilio">Twilio</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label htmlFor="areaCode">Area Code</Label>
              <div className="flex gap-2">
                <Input
                  id="areaCode"
                  placeholder="e.g., 415"
                  value={searchAreaCode}
                  onChange={(e) => setSearchAreaCode(e.target.value.replace(/\D/g, "").slice(0, 3))}
                  maxLength={3}
                />
                <Button onClick={() => void searchAvailableNumbersHandler()} disabled={isSearching}>
                  {isSearching ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Search className="h-4 w-4" />
                  )}
                </Button>
              </div>
            </div>

            {availableNumbers.length > 0 && (
              <div className="space-y-2">
                <Label>Available Numbers</Label>
                <div className="max-h-[200px] space-y-2 overflow-y-auto rounded-md border p-2">
                  {availableNumbers.map((num) => (
                    <div
                      key={num.phone_number}
                      onClick={() => setSelectedAvailableNumber(num.phone_number)}
                      className={`flex cursor-pointer items-center justify-between rounded-md p-2 transition-colors ${
                        selectedAvailableNumber === num.phone_number
                          ? "bg-primary text-primary-foreground"
                          : "hover:bg-accent"
                      }`}
                    >
                      <div>
                        <p className="font-mono font-medium">{num.phone_number}</p>
                        {num.friendly_name && (
                          <p className="text-xs opacity-80">{num.friendly_name}</p>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setIsPurchaseModalOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => void purchaseNumberHandler()}
              disabled={!selectedAvailableNumber || isPurchasing}
            >
              {isPurchasing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Purchase Number
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Details Modal */}
      <Dialog open={isDetailsModalOpen} onOpenChange={setIsDetailsModalOpen}>
        <DialogContent className="sm:max-w-[400px]">
          <DialogHeader>
            <DialogTitle>Phone Number Details</DialogTitle>
          </DialogHeader>
          {selectedNumber && (
            <div className="space-y-4 py-4">
              <div className="space-y-1">
                <Label className="text-muted-foreground">Phone Number</Label>
                <p className="font-mono text-lg font-medium">{selectedNumber.phoneNumber}</p>
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground">Provider</Label>
                <p className="capitalize">{selectedNumber.provider}</p>
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground">Assigned Agent</Label>
                <p>{selectedNumber.agentName ?? "Unassigned"}</p>
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground">Status</Label>
                <Badge variant={selectedNumber.isActive ? "default" : "secondary"}>
                  {selectedNumber.isActive ? "Active" : "Inactive"}
                </Badge>
              </div>
              <div className="space-y-1">
                <Label className="text-muted-foreground">Webhook URL</Label>
                <p className="break-all font-mono text-xs text-muted-foreground">
                  {`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/webhooks/${selectedNumber.provider}/voice`}
                </p>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsDetailsModalOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Assign Agent Modal */}
      <Dialog open={isAssignModalOpen} onOpenChange={setIsAssignModalOpen}>
        <DialogContent className="sm:max-w-[400px]">
          <DialogHeader>
            <DialogTitle>Assign to Agent</DialogTitle>
            <DialogDescription>
              Select an agent to handle calls on {selectedNumber?.phoneNumber}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>Select Agent</Label>
              <Select
                onValueChange={(value) => void handleAssignAgent(value)}
                disabled={isAssigning}
              >
                <SelectTrigger>
                  <SelectValue placeholder="Choose an agent" />
                </SelectTrigger>
                <SelectContent>
                  {agents.map((agent) => (
                    <SelectItem key={agent.id} value={agent.id}>
                      {agent.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {agents.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  No agents available. Create an agent first.
                </p>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setIsAssignModalOpen(false)}>
              Cancel
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Release Number Confirmation Dialog */}
      <AlertDialog open={isReleaseDialogOpen} onOpenChange={setIsReleaseDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Release Phone Number</AlertDialogTitle>
            <AlertDialogDescription>
              Are you sure you want to release {numberToRelease?.phoneNumber}? This action cannot be
              undone and you may not be able to get this number back.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => void confirmReleaseNumber()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              Release
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
