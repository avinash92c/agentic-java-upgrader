package main

import (
	"context"
	"fmt"
	"log"

	"github.com/tmc/langchaingo/llms"
	"github.com/tmc/langchaingo/llms/ollama"
	"github.com/tmc/langgraphgo/graph"
)

func main() {
	fmt.Println(`Hello`)

	model, err := ollama.New(
		ollama.WithServerURL("http://localhost:11434"),
		ollama.WithModel("codellama:7b"),
	)
	log.Println(err)

	// res, err := llm.Call(context.TODO(), `What is the time now`)
	// log.Println(err)
	// log.Println(res)

	g := graph.NewMessageGraph()

	g.AddNode("oracle", func(ctx context.Context, state []llms.MessageContent) ([]llms.MessageContent, error) {
		r, err := model.GenerateContent(ctx, state, llms.WithTemperature(0.0))
		if err != nil {
			return nil, err
		}
		return append(state,
			llms.TextParts(llms.ChatMessageTypeAI, r.Choices[0].Content),
		), nil

	})
	g.AddNode(graph.END, func(ctx context.Context, state []llms.MessageContent) ([]llms.MessageContent, error) {
		return state, nil
	})

	g.AddEdge("oracle", graph.END)
	g.SetEntryPoint("oracle")

	runnable, err := g.Compile()
	if err != nil {
		panic(err)
	}

	ctx := context.Background()
	// Let's run it!
	res, err := runnable.Invoke(ctx, []llms.MessageContent{
		llms.TextParts(llms.ChatMessageTypeHuman, "What is 1 + 1?"),
	})
	if err != nil {
		panic(err)
	}

	fmt.Println(res)

}
