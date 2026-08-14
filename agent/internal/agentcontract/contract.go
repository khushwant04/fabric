// Package agentcontract holds types shared between the agent and its publishers.
//
// It exists to break an import cycle: the agent imports a publisher to declare intent,
// and the publisher must describe what the cluster observed in a shape the agent
// understands. Putting that one type here keeps the dependency one-directional instead
// of introducing an interface assertion in both packages.
package agentcontract

// ObservedCondition is what a cluster reported about one deployment.
type ObservedCondition struct {
	Reason             string
	Message            string
	Applied            bool
	ObservedGeneration int64
}
