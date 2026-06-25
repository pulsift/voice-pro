import { describe, it, expect } from "vitest";
import { render, screen } from "@/test/test-utils";
import { AppSidebar } from "../app-sidebar";

// Helper to render sidebar
const renderSidebar = () => {
  return render(<AppSidebar />);
};

describe("AppSidebar", () => {
  it("renders component without crashing", () => {
    const { container } = renderSidebar();
    expect(container).toBeTruthy();
  });

  it("contains application branding", () => {
    renderSidebar();
    expect(screen.getByText("Voice Pro")).toBeInTheDocument();
  });

  it("renders navigation items", () => {
    renderSidebar();

    // Check for key navigation items (Settings is in dropdown menu, not main nav)
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    expect(screen.getByText("Voice Agents")).toBeInTheDocument();
    expect(screen.getByText("CRM")).toBeInTheDocument();
    expect(screen.getByText("Integrations")).toBeInTheDocument();
    expect(screen.getByText("Appointments")).toBeInTheDocument();
  });

  it("renders user profile information", () => {
    renderSidebar();

    expect(screen.getByText("User")).toBeInTheDocument();
    expect(screen.getByText("user@example.com")).toBeInTheDocument();
  });

  it("has navigation links with correct structure", () => {
    const { container } = renderSidebar();

    // Check for link elements
    const links = container.querySelectorAll("a");
    expect(links.length).toBeGreaterThan(5); // Should have multiple nav links
  });

  it("renders icons for navigation items", () => {
    const { container } = renderSidebar();

    // lucide-react icons render as SVG elements
    const svgIcons = container.querySelectorAll("svg");
    expect(svgIcons.length).toBeGreaterThan(0);
  });
});
